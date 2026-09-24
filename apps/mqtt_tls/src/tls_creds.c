/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Credentials embedded at build time (see CMakeLists.txt). PEM data must be
 * NUL-terminated for mbedTLS to parse it, and the terminator is part of the
 * length passed to tls_credential_add().
 *
 * tls_credential_add() only stores pointers; mbedTLS parses the data inside
 * connect(). A bad certificate would therefore show up as an endless series
 * of failed connection attempts. app_tls_creds_register() parses everything
 * once at boot instead, so misconfiguration is reported immediately and
 * precisely.
 */

#include <zephyr/logging/log.h>
#include <zephyr/net/tls_credentials.h>
#include <zephyr/random/random.h>

#include <mbedtls/pk.h>
#include <mbedtls/x509_crt.h>

#include "app.h"

LOG_MODULE_DECLARE(app, CONFIG_APP_LOG_LEVEL);

static const unsigned char ca_cert[] = {
#include "ca_cert.inc"
#if defined(APP_CA_CERT_IS_PEM)
	0x00
#endif
};

#if defined(APP_HAVE_CLIENT_CERT)
static const unsigned char client_cert[] = {
#include "client_cert.inc"
#if defined(APP_CLIENT_CERT_IS_PEM)
	0x00
#endif
};

static const unsigned char client_key[] = {
#include "client_key.inc"
#if defined(APP_CLIENT_KEY_IS_PEM)
	0x00
#endif
};

static int csrand(void *ctx, unsigned char *buf, size_t len)
{
	ARG_UNUSED(ctx);

	return sys_csrand_get(buf, len);
}
#endif /* APP_HAVE_CLIENT_CERT */

static int validate_credentials(void)
{
	mbedtls_x509_crt crt;
	int ret;

	mbedtls_x509_crt_init(&crt);
	ret = mbedtls_x509_crt_parse(&crt, ca_cert, sizeof(ca_cert));
	if (ret != 0) {
		/* A common cause is a root self-signed with SHA-1 (e.g.
		 * DigiCert Global Root CA): SHA-1 is not built in. Configure
		 * a SHA-256 root or intermediate as trust anchor instead.
		 */
		LOG_ERR("CA certificate %s cannot be parsed: -0x%04x",
			CONFIG_APP_MQTT_TLS_CA_CERT_FILE, (unsigned int)-ret);
		mbedtls_x509_crt_free(&crt);
		return -EINVAL;
	}
	mbedtls_x509_crt_free(&crt);

#if defined(APP_HAVE_CLIENT_CERT)
	mbedtls_pk_context key;

	mbedtls_x509_crt_init(&crt);
	mbedtls_pk_init(&key);

	ret = mbedtls_x509_crt_parse(&crt, client_cert, sizeof(client_cert));
	if (ret != 0) {
		LOG_ERR("Client certificate cannot be parsed: -0x%04x", (unsigned int)-ret);
		goto out;
	}

	ret = mbedtls_pk_parse_key(&key, client_key, sizeof(client_key), NULL, 0,
				   csrand, NULL);
	if (ret != 0) {
		LOG_ERR("Client private key cannot be parsed (encrypted keys are not "
			"supported): -0x%04x", (unsigned int)-ret);
		goto out;
	}

	ret = mbedtls_pk_check_pair(&crt.pk, &key, csrand, NULL);
	if (ret != 0) {
		LOG_ERR("Client private key does not match the client certificate: -0x%04x",
			(unsigned int)-ret);
	}

out:
	mbedtls_pk_free(&key);
	mbedtls_x509_crt_free(&crt);
	if (ret != 0) {
		return -EINVAL;
	}
#endif /* APP_HAVE_CLIENT_CERT */

	return 0;
}

int app_tls_creds_register(void)
{
	int ret;

	ret = validate_credentials();
	if (ret < 0) {
		return ret;
	}

	ret = tls_credential_add(APP_TLS_SEC_TAG, TLS_CREDENTIAL_CA_CERTIFICATE,
				 ca_cert, sizeof(ca_cert));
	if (ret < 0) {
		LOG_ERR("Failed to register CA certificate: %d", ret);
		return ret;
	}

#if defined(APP_HAVE_CLIENT_CERT)
	/* Zephyr uses the "server certificate" slot for the local end's own
	 * certificate, whether it acts as TLS server or client.
	 */
	ret = tls_credential_add(APP_TLS_SEC_TAG,
				 TLS_CREDENTIAL_SERVER_CERTIFICATE,
				 client_cert, sizeof(client_cert));
	if (ret < 0) {
		LOG_ERR("Failed to register client certificate: %d", ret);
		return ret;
	}

	ret = tls_credential_add(APP_TLS_SEC_TAG, TLS_CREDENTIAL_PRIVATE_KEY,
				 client_key, sizeof(client_key));
	if (ret < 0) {
		LOG_ERR("Failed to register client private key: %d", ret);
		return ret;
	}

	LOG_INF("TLS credentials registered (CA + client certificate)");
#else
	LOG_INF("TLS credentials registered (CA only)");
#endif

	return 0;
}
