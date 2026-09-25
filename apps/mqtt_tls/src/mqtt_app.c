/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * MQTT 3.1.1 session handling: connect over TLS, subscribe to the command
 * topic, publish telemetry periodically, and detect dead connections.
 *
 * Everything runs on the calling (main) thread; the MQTT library invokes
 * mqtt_evt_handler() from inside mqtt_input()/mqtt_connect(), so no locking
 * is needed around the session state.
 *
 * No call in here may block without bound: connect() is limited by
 * CONFIG_NET_SOCKETS_CONNECT_TIMEOUT, the TLS handshake by
 * CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT, every other socket read
 * and write by SO_RCVTIMEO/SO_SNDTIMEO (see set_socket_timeouts()), and the
 * watchdog is fed on every loop iteration as a backstop.
 */

#include <errno.h>
#include <stdio.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/net/mqtt.h>
#include <zephyr/net/net_if.h>
#include <zephyr/net/socket.h>
#include <zephyr/sys/atomic.h>
#include <zephyr/sys/util.h>
#include <zephyr/version.h>
#include <zephyr/app_version.h>

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

#define TOPIC_LEN 128
#define CLIENT_ID_LEN 64

/* Poll at least this often so network loss and timers are noticed promptly. */
#define MAX_POLL_INTERVAL_MS 1000

/* Time to wait for PINGRESP before declaring the connection dead. Also the
 * bound for any single blocking socket read or write inside a session.
 */
#define PINGRESP_TIMEOUT_MS ((int)MIN(cfg.keepalive, 30) * MSEC_PER_SEC)
#define SOCKET_IO_TIMEOUT_MS PINGRESP_TIMEOUT_MS

/* A session must stay up this long before the caller resets its back-off,
 * so a broker that accepts and immediately drops us (duplicate client id,
 * ACL denial) does not cause a tight reconnect loop.
 */
#define HEALTHY_SESSION_MS (30 * MSEC_PER_SEC)

#define STATUS_ONLINE "online"
#define STATUS_OFFLINE "offline"

BUILD_ASSERT(CONFIG_MQTT_KEEPALIVE > 0 && CONFIG_MQTT_KEEPALIVE <= UINT16_MAX,
	     "MQTT keep-alive must be 1..65535 s (it is also used for dead-peer detection)");
BUILD_ASSERT(sizeof(CONFIG_APP_MQTT_PASSWORD) == 1 || sizeof(CONFIG_APP_MQTT_USERNAME) > 1,
	     "MQTT 3.1.1 does not allow a password without a user name");
BUILD_ASSERT(CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE > 0, "payload buffer must not be empty");

static uint8_t rx_buffer[CONFIG_APP_MQTT_BUFFER_SIZE];
static uint8_t tx_buffer[CONFIG_APP_MQTT_BUFFER_SIZE];
static uint8_t payload_buf[CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE + 1];

static struct mqtt_client client;
static struct net_sockaddr_storage broker;

static char client_id[CLIENT_ID_LEN];
static char topic_status[TOPIC_LEN];
static char topic_info[TOPIC_LEN];
static char topic_telemetry[TOPIC_LEN];
static char topic_cmd[TOPIC_LEN];
static char topic_event[TOPIC_LEN];

static struct mqtt_topic will_topic;
static struct mqtt_utf8 will_message = {
	.utf8 = (const uint8_t *)STATUS_OFFLINE,
	.size = sizeof(STATUS_OFFLINE) - 1,
};
static struct mqtt_utf8 username;
static struct mqtt_utf8 password;
static char topic_log[TOPIC_LEN];

/* Configuration snapshot for the current session (see config.c). */
static struct app_config cfg;
static uint32_t cfg_mask; /* stored keys in cfg, for the last known good */

static atomic_t reconnect_requested;

/* Log lines already published (running line number, see log_mqtt.c). It
 * survives reconnects, so every line is published once, including those
 * from before the first connection.
 */
static uint32_t log_cursor;
static int log_tokens = CONFIG_APP_LOG_MQTT_BURST;
static int64_t log_refill_at;

#if defined(CONFIG_APP_MQTT_TLS)
static const sec_tag_t sec_tags[] = { APP_TLS_SEC_TAG };
#endif

static struct {
	bool connected;       /* CONNACK accepted, no DISCONNECT since */
	bool connack_failed;  /* CONNACK refused or malformed */
	bool subscribed;      /* SUBACK granted the command subscription */
	bool subscribe_failed;
	uint16_t subscribe_msg_id;
	bool ping_outstanding;
	int64_t ping_sent_at;
	int64_t last_rx;
	uint16_t last_msg_id;
	uint32_t telemetry_seq;
	uint32_t sessions;
} s;

static uint16_t next_message_id(void)
{
	/* Message identifiers must be non-zero (MQTT 3.1.1, 2.3.1). */
	if (++s.last_msg_id == 0U) {
		s.last_msg_id = 1U;
	}

	return s.last_msg_id;
}

static int mqtt_sock(void)
{
#if defined(CONFIG_APP_MQTT_TLS)
	return client.transport.tls.sock;
#else
	return client.transport.tcp.sock;
#endif
}

static int publish(const char *topic, const void *data, size_t len,
		   enum mqtt_qos qos, bool retain)
{
	struct mqtt_publish_param param = { 0 };

	param.message.topic.topic.utf8 = (const uint8_t *)topic;
	param.message.topic.topic.size = strlen(topic);
	param.message.topic.qos = qos;
	param.message.payload.data = (uint8_t *)data;
	param.message.payload.len = len;
	param.message_id = (qos == MQTT_QOS_0_AT_MOST_ONCE) ? 0U : next_message_id();
	param.dup_flag = 0U;
	param.retain_flag = retain ? 1U : 0U;

	return mqtt_publish(&client, &param);
}

static int publish_str(const char *topic, const char *str, enum mqtt_qos qos,
		       bool retain)
{
	return publish(topic, str, strlen(str), qos, retain);
}

static void link_addr_str(struct net_if *iface, char *buf, size_t len)
{
	struct net_linkaddr *ll = (iface != NULL) ? net_if_get_link_addr(iface) : NULL;

	if (ll == NULL || ll->len != 6U) {
		strncpy(buf, "unknown", len - 1);
		buf[len - 1] = '\0';
		return;
	}

	snprintk(buf, len, "%02x:%02x:%02x:%02x:%02x:%02x", ll->addr[0], ll->addr[1],
		 ll->addr[2], ll->addr[3], ll->addr[4], ll->addr[5]);
}

/* Retained description of the device, used by the uc-hub gateway to build
 * its point list (see README "Topics").
 */
static int publish_info(void)
{
	static char payload[1024];
	char ip[NET_IPV4_ADDR_LEN] = "unknown";
	char mac[18];
	char hwid[33];
	char caps[384];
	char mgmt[80];
	struct net_if *iface = net_if_get_default();
	struct net_in_addr *addr = NULL;
	int len;

	if (iface != NULL) {
		addr = net_if_ipv4_get_global_addr(iface, NET_ADDR_PREFERRED);
	}
	if (addr != NULL) {
		net_addr_ntop(NET_AF_INET, addr, ip, sizeof(ip));
	}
	link_addr_str(iface, mac, sizeof(mac));
	app_hwid_get(hwid, sizeof(hwid));
	app_commands_caps(caps, sizeof(caps));
	app_mgmt_info_json(mgmt, sizeof(mgmt));

	len = snprintk(payload, sizeof(payload),
		       "{\"fw\":\"%s\",\"board\":\"%s\",\"zephyr\":\"%s\","
		       "\"hwid\":\"%s\",\"mac\":\"%s\",\"ip\":\"%s\",\"tls\":%s,%s,\"caps\":%s}",
		       APP_VERSION_STRING, CONFIG_BOARD, KERNEL_VERSION_STRING, hwid, mac, ip,
		       IS_ENABLED(CONFIG_APP_MQTT_TLS) ? "true" : "false", mgmt, caps);
	if (len >= (int)sizeof(payload)) {
		return -ENOMEM;
	}

	return publish_str(topic_info, payload, MQTT_QOS_1_AT_LEAST_ONCE, true);
}

static int publish_telemetry(void)
{
	char payload[128];
	int ret;

	snprintk(payload, sizeof(payload),
		 "{\"seq\":%u,\"uptime_s\":%u,\"sessions\":%u}",
		 s.telemetry_seq, k_uptime_seconds(), s.sessions);

	ret = publish_str(topic_telemetry, payload,
			  (enum mqtt_qos)CONFIG_APP_MQTT_PUBLISH_QOS, false);
	if (ret == 0) {
		LOG_INF("Published telemetry #%u", s.telemetry_seq);
		s.telemetry_seq++;
	}

	return ret;
}

static int subscribe_commands(void)
{
	struct mqtt_topic topic = {
		.topic = {
			.utf8 = (const uint8_t *)topic_cmd,
			.size = strlen(topic_cmd),
		},
		.qos = MQTT_QOS_1_AT_LEAST_ONCE,
	};
	struct mqtt_subscription_list list = {
		.list = &topic,
		.list_count = 1U,
		.message_id = next_message_id(),
	};

	s.subscribe_msg_id = list.message_id;
	LOG_INF("Subscribing to %s", topic_cmd);

	return mqtt_subscribe(&client, &list);
}

static bool topic_equals(const struct mqtt_utf8 *t, const char *str)
{
	size_t len = strlen(str);

	return t->size == len && memcmp(t->utf8, str, len) == 0;
}

/* Read the payload of an incoming PUBLISH. Oversized payloads are drained
 * so that the stream stays aligned on MQTT packet boundaries. Each read is
 * bounded by SO_RCVTIMEO, so a peer that stalls mid-payload ends the session
 * instead of wedging the thread.
 * Returns the payload length, -EMSGSIZE if it was discarded, or another
 * negative errno if the connection failed.
 */
static int read_payload(const struct mqtt_publish_param *pub)
{
	uint32_t len = pub->message.payload.len;
	int ret;

	if (len <= CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE) {
		ret = mqtt_readall_publish_payload(&client, payload_buf, len);
		if (ret < 0) {
			return ret;
		}
		payload_buf[len] = '\0';
		return (int)len;
	}

	while (len > 0U) {
		app_wdt_feed();
		ret = mqtt_read_publish_payload_blocking(
			&client, payload_buf,
			MIN(len, CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE));
		if (ret < 0) {
			return ret;
		}
		if (ret == 0) {
			return -EIO;
		}
		len -= ret;
	}

	return -EMSGSIZE;
}

static void handle_publish(const struct mqtt_publish_param *pub)
{
	const struct mqtt_utf8 *topic = &pub->message.topic.topic;
	/* MQTT thread only; leave room in tx_buffer for the topic and header. */
	static char reply[CONFIG_APP_MQTT_BUFFER_SIZE - 192];
	int len;
	int ret;

	len = read_payload(pub);
	if (len < 0 && len != -EMSGSIZE) {
		LOG_ERR("Failed to read payload: %d", len);
		/* The library has already torn the connection down. */
		return;
	}

	/* Acknowledge before acting on the message, as QoS 1 permits. */
	if (pub->message.topic.qos == MQTT_QOS_1_AT_LEAST_ONCE) {
		struct mqtt_puback_param ack = { .message_id = pub->message_id };

		ret = mqtt_publish_qos1_ack(&client, &ack);
		if (ret < 0) {
			LOG_ERR("PUBACK failed: %d", ret);
		}
	} else if (pub->message.topic.qos == MQTT_QOS_2_EXACTLY_ONCE) {
		struct mqtt_pubrec_param rec = { .message_id = pub->message_id };

		ret = mqtt_publish_qos2_receive(&client, &rec);
		if (ret < 0) {
			LOG_ERR("PUBREC failed: %d", ret);
		}
	}

	if (!topic_equals(topic, topic_cmd)) {
		char name[64];
		size_t n = MIN(topic->size, sizeof(name) - 1);

		/* The topic in rx_buffer is not NUL-terminated. */
		memcpy(name, topic->utf8, n);
		name[n] = '\0';
		LOG_WRN("Ignoring message on unexpected topic %s", name);
		return;
	}

	/* Live messages are always delivered with RETAIN=0 [MQTT-3.3.1-9].
	 * RETAIN=1 means a stale command stored on the broker, which would
	 * otherwise be replayed after every reconnect.
	 */
	if (pub->retain_flag) {
		LOG_WRN("Ignoring retained command (commands must not be retained)");
		return;
	}

	/* Clearing a retained message publishes an empty payload. */
	if (len == 0) {
		LOG_INF("Ignoring empty command");
		return;
	}

	if (len == -EMSGSIZE) {
		LOG_WRN("Command of %u bytes discarded (max %d)",
			pub->message.payload.len, CONFIG_APP_MQTT_MAX_PAYLOAD_SIZE);
		snprintk(reply, sizeof(reply), "{\"ok\":false,\"error\":\"payload too large\"}");
	} else {
		/* Never log secrets (the log may be published on MQTT). */
		LOG_INF("Command: %s", strstr((const char *)payload_buf, "password") != NULL
					       ? "(redacted, contains a password)"
					       : (const char *)payload_buf);
		app_handle_command((char *)payload_buf, reply, sizeof(reply));
	}

	ret = publish_str(topic_event, reply, MQTT_QOS_1_AT_LEAST_ONCE, false);
	if (ret < 0) {
		LOG_ERR("Failed to publish reply: %d", ret);
	}
}

static const char *connack_reason(int code)
{
	switch (code) {
	case MQTT_UNACCEPTABLE_PROTOCOL_VERSION:
		return "unacceptable protocol version";
	case MQTT_IDENTIFIER_REJECTED:
		return "client identifier rejected (too long or invalid characters?)";
	case MQTT_SERVER_UNAVAILABLE:
		return "server unavailable";
	case MQTT_BAD_USER_NAME_OR_PASSWORD:
		return "bad user name or password";
	case MQTT_NOT_AUTHORIZED:
		return "not authorized";
	default:
		return "malformed CONNACK";
	}
}

static void mqtt_evt_handler(struct mqtt_client *const c,
			     const struct mqtt_evt *evt)
{
	switch (evt->type) {
	case MQTT_EVT_CONNACK:
		if (evt->result != 0) {
			/* result is the CONNACK return code (1..5) or a
			 * negative errno for a malformed packet.
			 */
			LOG_ERR("Broker refused connection: %d (%s)", evt->result,
				connack_reason(evt->result));
			s.connack_failed = true;
			break;
		}
		s.connected = true;
		LOG_INF("MQTT session established (session present: %d)",
			evt->param.connack.session_present_flag);
		break;

	case MQTT_EVT_DISCONNECT:
		LOG_INF("MQTT disconnected: %d", evt->result);
		s.connected = false;
		break;

	case MQTT_EVT_PUBLISH:
		if (evt->result != 0) {
			LOG_ERR("PUBLISH error: %d", evt->result);
			break;
		}
		handle_publish(&evt->param.publish);
		break;

	case MQTT_EVT_PUBACK:
		if (evt->result != 0) {
			LOG_ERR("PUBACK error: %d", evt->result);
			break;
		}
		LOG_DBG("PUBACK id %u", evt->param.puback.message_id);
		break;

	case MQTT_EVT_PUBREL: {
		/* Last step of an inbound QoS 2 flow. */
		struct mqtt_pubcomp_param comp = {
			.message_id = evt->param.pubrel.message_id,
		};

		(void)mqtt_publish_qos2_complete(c, &comp);
		break;
	}

	case MQTT_EVT_SUBACK: {
		const struct mqtt_suback_param *ack = &evt->param.suback;

		if (ack->message_id != s.subscribe_msg_id) {
			LOG_WRN("SUBACK for unknown message id %u", ack->message_id);
			break;
		}
		if (evt->result != 0 || ack->return_codes.len == 0U ||
		    ack->return_codes.data[0] == MQTT_SUBACK_FAILURE) {
			LOG_ERR("Subscription to %s rejected by the broker (check its ACL)",
				topic_cmd);
			s.subscribe_failed = true;
		} else {
			LOG_INF("Subscribed (granted QoS %u)", ack->return_codes.data[0]);
			s.subscribed = true;
		}
		break;
	}

	case MQTT_EVT_PINGRESP:
		LOG_DBG("PINGRESP");
		s.ping_outstanding = false;
		break;

	default:
		break;
	}
}

static int resolve_broker(void)
{
	static const struct zsock_addrinfo hints = {
		.ai_family = NET_AF_INET,
		.ai_socktype = NET_SOCK_STREAM,
	};
	struct zsock_addrinfo *res = NULL;
	char port[6];
	char addr_str[NET_IPV4_ADDR_LEN];
	int ret;

	snprintk(port, sizeof(port), "%u", cfg.broker_port);

	ret = zsock_getaddrinfo(cfg.broker_host, port, &hints, &res);
	if (ret != 0 || res == NULL) {
		LOG_ERR("Cannot resolve %s: %d", cfg.broker_host, ret);
		return -EHOSTUNREACH;
	}

	memset(&broker, 0, sizeof(broker));
	memcpy(&broker, res->ai_addr, MIN(res->ai_addrlen, sizeof(broker)));
	zsock_freeaddrinfo(res);

	LOG_INF("Broker %s -> %s:%u", cfg.broker_host,
		net_addr_ntop(NET_AF_INET, &net_sin(net_sad(&broker))->sin_addr,
			      addr_str, sizeof(addr_str)),
		cfg.broker_port);

	return 0;
}

static void client_setup(void)
{
	mqtt_client_init(&client);

	client.broker = &broker;
	client.evt_cb = mqtt_evt_handler;
	client.client_id.utf8 = (const uint8_t *)client_id;
	client.client_id.size = strlen(client_id);
	client.protocol_version = MQTT_VERSION_3_1_1;
	client.keepalive = cfg.keepalive;
	/* The application keeps no state across connections (it
	 * re-subscribes and does not resend in-flight messages), so it
	 * always asks for a clean session.
	 */
	client.clean_session = 1U;

	client.will_topic = &will_topic;
	client.will_message = &will_message;
	client.will_retain = 1U;

	username.utf8 = (const uint8_t *)cfg.username;
	username.size = strlen(cfg.username);
	password.utf8 = (const uint8_t *)cfg.password;
	password.size = strlen(cfg.password);
	client.user_name = (username.size > 0U) ? &username : NULL;
	client.password = (password.size > 0U) ? &password : NULL;
	if (client.password != NULL && client.user_name == NULL) {
		/* MQTT 3.1.1 [MQTT-3.1.2-22] */
		LOG_WRN("Password configured without a user name: not sent");
		client.password = NULL;
	}

	client.rx_buf = rx_buffer;
	client.rx_buf_size = sizeof(rx_buffer);
	client.tx_buf = tx_buffer;
	client.tx_buf_size = sizeof(tx_buffer);

#if defined(CONFIG_APP_MQTT_TLS)
	struct mqtt_sec_config *tls = &client.transport.tls.config;

	client.transport.type = MQTT_TRANSPORT_SECURE;
	tls->peer_verify = IS_ENABLED(CONFIG_APP_MQTT_TLS_PEER_VERIFY)
				   ? ZSOCK_TLS_PEER_VERIFY_REQUIRED
				   : ZSOCK_TLS_PEER_VERIFY_NONE;
	tls->cipher_list = NULL;
	tls->cipher_count = 0U;
	tls->sec_tag_list = sec_tags;
	tls->sec_tag_count = ARRAY_SIZE(sec_tags);
	/* Used for SNI and for matching the broker certificate. */
	tls->hostname = (cfg.tls_hostname[0] != '\0') ? cfg.tls_hostname : cfg.broker_host;
	tls->cert_nocopy = ZSOCK_TLS_CERT_NOCOPY_NONE;
#else
	client.transport.type = MQTT_TRANSPORT_NON_SECURE;
#endif
}

/* Bound every blocking read and write on the session socket. Without this a
 * TLS socket waits forever (its default timeouts are K_FOREVER), e.g. when
 * the peer stalls in the middle of a PUBLISH payload.
 */
static int set_socket_timeouts(void)
{
	struct zsock_timeval tv = {
		.tv_sec = SOCKET_IO_TIMEOUT_MS / MSEC_PER_SEC,
		.tv_usec = (SOCKET_IO_TIMEOUT_MS % MSEC_PER_SEC) * USEC_PER_MSEC,
	};

	if (zsock_setsockopt(mqtt_sock(), ZSOCK_SOL_SOCKET, ZSOCK_SO_RCVTIMEO, &tv, sizeof(tv)) < 0 ||
	    zsock_setsockopt(mqtt_sock(), ZSOCK_SOL_SOCKET, ZSOCK_SO_SNDTIMEO, &tv, sizeof(tv)) < 0) {
		LOG_ERR("Cannot set socket timeouts: %d", -errno);
		return -errno;
	}

	return 0;
}

/* Wait until the MQTT socket is readable or @p timeout_ms passes.
 * Returns 1 if readable, 0 on timeout, negative errno on socket error.
 */
static int wait_readable(int timeout_ms)
{
	struct zsock_pollfd fds = {
		.fd = mqtt_sock(),
		.events = ZSOCK_POLLIN,
	};
	int ret;

	ret = zsock_poll(&fds, 1, MAX(timeout_ms, 0));
	if (ret < 0) {
		return -errno;
	}
	if (ret == 0) {
		return 0;
	}
	if (fds.revents & (ZSOCK_POLLERR | ZSOCK_POLLNVAL)) {
		return -EIO;
	}
	if (fds.revents & ZSOCK_POLLHUP) {
		/* Still let mqtt_input() drain whatever arrived before the
		 * close; it reports the disconnect itself.
		 */
		return 1;
	}

	return (fds.revents & ZSOCK_POLLIN) ? 1 : 0;
}

static int input(void)
{
	int ret = mqtt_input(&client);

	if (ret == 0) {
		s.last_rx = k_uptime_get();
	}

	return ret;
}

/* Process incoming packets until *done or *failed becomes true, the
 * connection drops, or @p timeout_ms passes.
 */
static int pump_until(const bool *done, const bool *failed, int timeout_ms,
		      const char *what)
{
	int64_t deadline = k_uptime_get() + timeout_ms;
	int ret;

	while (!*done) {
		int64_t remaining = deadline - k_uptime_get();

		app_wdt_feed();

		if (*failed) {
			return -ECONNREFUSED;
		}
		if (remaining <= 0) {
			LOG_ERR("No %s within %d ms", what, timeout_ms);
			return -ETIMEDOUT;
		}

		ret = wait_readable((int)MIN(remaining, MAX_POLL_INTERVAL_MS));
		if (ret < 0) {
			return ret;
		}
		if (ret == 0) {
			continue;
		}

		ret = input();
		if (*failed) {
			return -ECONNREFUSED;
		}
		if (ret < 0) {
			return ret;
		}
	}

	return 0;
}

/* Detect a silently dead connection (cable pulled upstream, NAT timeout):
 * TCP alone only notices when its retransmissions give up, which takes
 * minutes. Ping when nothing has been received for a keep-alive period and
 * give up if the PINGRESP does not arrive in time.
 */
static int check_liveness(int64_t now)
{
	int ret;

	ret = mqtt_live(&client);
	if (ret == 0 && !s.ping_outstanding) {
		s.ping_outstanding = true;
		s.ping_sent_at = now;
	} else if (ret < 0 && ret != -EAGAIN) {
		LOG_ERR("Keep-alive failed: %d", ret);
		return ret;
	}

	if (!s.ping_outstanding &&
	    now - s.last_rx >= (int64_t)cfg.keepalive * MSEC_PER_SEC) {
		ret = mqtt_ping(&client);
		if (ret < 0) {
			LOG_ERR("PINGREQ failed: %d", ret);
			return ret;
		}
		s.ping_outstanding = true;
		s.ping_sent_at = now;
	}

	if (s.ping_outstanding && now - s.ping_sent_at >= PINGRESP_TIMEOUT_MS) {
		LOG_ERR("No PINGRESP within %d ms, connection is dead", PINGRESP_TIMEOUT_MS);
		return -ETIMEDOUT;
	}

	return 0;
}

/* Publish captured log lines, rate-limited by a token bucket
 * (APP_LOG_MQTT_RATE lines per second, bursts up to APP_LOG_MQTT_BURST).
 */
static int publish_logs(int64_t now)
{
	static char payload[320];
	/* Lines lost before a line whose publish failed: reported with it. */
	static uint32_t pending_lost;
	struct app_log_line line;
	uint32_t lost;
	int ret;

	if (now >= log_refill_at) {
		log_tokens = MIN(log_tokens + CONFIG_APP_LOG_MQTT_RATE, CONFIG_APP_LOG_MQTT_BURST);
		log_refill_at = now + MSEC_PER_SEC;
	}

	while (log_tokens > 0 && app_log_next(&log_cursor, &line, &lost)) {
		lost += pending_lost;
		pending_lost = 0U;
		ret = app_log_line_json(&line, lost, payload, sizeof(payload));
		if (ret < 0) {
			continue;
		}
		ret = publish(topic_log, payload, ret, MQTT_QOS_0_AT_MOST_ONCE, false);
		if (ret < 0) {
			/* Re-read this line in the next session. */
			log_cursor--;
			pending_lost = lost;
			return ret;
		}
		log_tokens--;
	}

	return 0;
}

static int64_t publish_interval_ms(void)
{
	struct app_config now_cfg;

	/* Applies immediately, unlike the connection settings. */
	app_config_get(&now_cfg);

	return (int64_t)now_cfg.publish_interval * MSEC_PER_SEC;
}

static int serve(void)
{
	int64_t next_publish = k_uptime_get();
	int ret;

	while (true) {
		int64_t now = k_uptime_get();
		int timeout;

		app_wdt_feed();

		if (!s.connected) {
			return -ENOTCONN;
		}
		if (!app_net_is_up()) {
			LOG_WRN("Network down, closing MQTT session");
			return -ENETDOWN;
		}

		if (atomic_cas(&reconnect_requested, 1, 0)) {
			LOG_INF("Reconnecting on request");
			return -ECONNRESET;
		}

		if (now >= next_publish) {
			int64_t interval_ms = publish_interval_ms();

			ret = publish_telemetry();
			if (ret < 0) {
				LOG_ERR("Telemetry publish failed: %d", ret);
				return ret;
			}
			next_publish += interval_ms;
			if (next_publish > now + interval_ms) {
				/* The interval was shortened: don't wait out the old one. */
				next_publish = now + interval_ms;
			}
			if (next_publish <= now) {
				/* Fell behind (e.g. a long TLS stall): skip
				 * the missed slots instead of bursting.
				 */
				next_publish = now + interval_ms;
			}
		}

		ret = publish_logs(now);
		if (ret < 0) {
			LOG_ERR("Log publish failed: %d", ret);
			return ret;
		}

		ret = check_liveness(now);
		if (ret < 0) {
			return ret;
		}

		timeout = (int)MIN(next_publish - now, MAX_POLL_INTERVAL_MS);
		timeout = MIN(timeout, mqtt_keepalive_time_left(&client));

		ret = wait_readable(timeout);
		if (ret < 0) {
			LOG_ERR("Socket error: %d", ret);
			return ret;
		}
		if (ret > 0) {
			ret = input();
			if (ret < 0) {
				LOG_ERR("mqtt_input: %d", ret);
				return ret;
			}
		}
	}
}

static bool is_valid_topic_level(const char *str, const char *forbidden)
{
	return str[0] != '\0' && strpbrk(str, forbidden) == NULL;
}

int app_mqtt_init(void)
{
	int ret;

	ret = app_client_id_get(client_id, sizeof(client_id));
	if (ret < 0) {
		LOG_ERR("Client ID does not fit: %d", ret);
		return ret;
	}

	/* The client id is also a topic level. */
	if (!is_valid_topic_level(client_id, "+#/")) {
		LOG_ERR("Client ID contains '+', '#' or '/'");
		return -EINVAL;
	}

	if (strlen(client_id) > 23) {
		LOG_WRN("Client ID is %u characters; MQTT 3.1.1 brokers only have to "
			"accept 23", (unsigned int)strlen(client_id));
	}

	LOG_INF("Client ID: %s", client_id);

#if defined(CONFIG_APP_MQTT_TLS) && !defined(CONFIG_APP_MQTT_TLS_PEER_VERIFY)
	LOG_WRN("Broker certificate is NOT verified: the connection can be intercepted");
#endif

	return 0;
}

/* Topics depend on the (runtime) topic root, so they are rebuilt for each
 * session. The root is validated when it is set (config.c).
 */
static int build_topics(void)
{
#define MAKE_TOPIC(buf, leaf)                                                   \
	(snprintk(buf, sizeof(buf), "%s/%s/" leaf, cfg.topic_root, client_id) >= \
	 (int)sizeof(buf))

	if (MAKE_TOPIC(topic_status, "status") || MAKE_TOPIC(topic_info, "info") ||
	    MAKE_TOPIC(topic_telemetry, "telemetry") || MAKE_TOPIC(topic_cmd, "cmd") ||
	    MAKE_TOPIC(topic_event, "event") || MAKE_TOPIC(topic_log, "log")) {
		LOG_ERR("Topic root too long");
		return -ENAMETOOLONG;
	}
#undef MAKE_TOPIC

	will_topic.topic.utf8 = (const uint8_t *)topic_status;
	will_topic.topic.size = strlen(topic_status);
	will_topic.qos = MQTT_QOS_1_AT_LEAST_ONCE;

	return 0;
}

/* After a topic_root change, remove the retained status and info that the
 * previous (last known good) root still holds, so the old device entry does
 * not linger as "offline" forever. Best effort.
 */
static void clear_old_root(void)
{
	char old_root[APP_CFG_STR_LEN];
	char topic[sizeof(old_root) + sizeof(client_id) + sizeof("/status")];

	app_config_lkg_topic_root(old_root, sizeof(old_root));
	if (old_root[0] == '\0' || strcmp(old_root, cfg.topic_root) == 0) {
		return;
	}

	LOG_INF("Topic root changed: clearing retained messages under %s/%s", old_root,
		client_id);
	(void)snprintf(topic, sizeof(topic), "%s/%s/status", old_root, client_id);
	(void)publish(topic, "", 0, MQTT_QOS_1_AT_LEAST_ONCE, true);
	(void)snprintf(topic, sizeof(topic), "%s/%s/info", old_root, client_id);
	(void)publish(topic, "", 0, MQTT_QOS_1_AT_LEAST_ONCE, true);
}

void app_mqtt_request_reconnect(void)
{
	atomic_set(&reconnect_requested, 1);
}

int app_mqtt_run_session(bool *was_connected)
{
	int64_t connected_at;
	int64_t t0;
	int ret;

	*was_connected = false;

	/* Counts down a trial of new connection settings, and rolls back to
	 * the last known good once it is used up.
	 */
	(void)app_config_attempt();
	app_config_snapshot(&cfg, &cfg_mask);
	atomic_clear(&reconnect_requested);

	ret = build_topics();
	if (ret < 0) {
		return ret;
	}

	app_wdt_feed();
	ret = resolve_broker();
	if (ret < 0) {
		return ret;
	}

	client_setup();
	s.connected = false;
	s.connack_failed = false;
	s.subscribed = false;
	s.subscribe_failed = false;
	s.ping_outstanding = false;

	LOG_INF("Connecting to %s:%u (%s), topics %s/%s/...", cfg.broker_host,
		cfg.broker_port, IS_ENABLED(CONFIG_APP_MQTT_TLS) ? "TLS" : "plain TCP",
		cfg.topic_root, client_id);

	/* Blocks for the TCP connect (CONFIG_NET_SOCKETS_CONNECT_TIMEOUT) and
	 * then the full TLS handshake (CONFIG_NET_SOCKETS_TLS_CONNECT_TIMEOUT),
	 * whose bound includes the certificate and ECDHE computations.
	 */
	app_wdt_feed();
	t0 = k_uptime_get();
	ret = mqtt_connect(&client);
	if (ret < 0) {
		if (ret == -EAGAIN || ret == -ETIMEDOUT) {
			LOG_ERR("TCP connect or TLS handshake timed out after %d ms",
				(int)(k_uptime_get() - t0));
		} else {
			LOG_ERR("mqtt_connect failed: %d", ret);
		}
		return ret;
	}
	LOG_INF("TCP + TLS connected in %d ms", (int)(k_uptime_get() - t0));
	s.last_rx = k_uptime_get();

	ret = set_socket_timeouts();
	if (ret < 0) {
		goto out;
	}

	ret = pump_until(&s.connected, &s.connack_failed,
			 CONFIG_APP_MQTT_CONNACK_TIMEOUT_MS, "CONNACK");
	if (ret < 0) {
		goto out;
	}

	connected_at = k_uptime_get();
	s.sessions++;

	ret = subscribe_commands();
	if (ret < 0) {
		LOG_ERR("Subscribe failed: %d", ret);
		goto out;
	}

	/* Only announce "online" once the device can actually take commands. */
	ret = pump_until(&s.subscribed, &s.subscribe_failed,
			 CONFIG_APP_MQTT_CONNACK_TIMEOUT_MS, "SUBACK");
	if (ret < 0) {
		goto out;
	}

	ret = publish_str(topic_status, STATUS_ONLINE, MQTT_QOS_1_AT_LEAST_ONCE, true);
	if (ret == 0) {
		ret = publish_info();
	}
	if (ret < 0) {
		LOG_ERR("Failed to publish status: %d", ret);
		goto out;
	}

	/* Online: this configuration works, and so does this image. */
	clear_old_root();
	app_config_online(&cfg, cfg_mask);
	app_mgmt_online();

	ret = serve();

	*was_connected = (k_uptime_get() - connected_at) >= HEALTHY_SESSION_MS;

out:
	/* No-op if the library already closed the socket. */
	(void)mqtt_abort(&client);
	s.connected = false;

	return ret;
}
