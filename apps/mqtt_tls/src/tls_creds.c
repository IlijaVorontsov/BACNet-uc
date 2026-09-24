/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Credentials embedded at build time (see CMakeLists.txt). PEM data must be
 * NUL-terminated for mbedTLS to parse it, and the terminator is part of the
 * length passed to tls_credential_add().
 */

#include <zephyr/logging/log.h>
#include <zephyr/net/tls_credentials.h>

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
#endif /* APP_HAVE_CLIENT_CERT */

int app_tls_creds_register(void)
{
	int ret;

	ret = tls_credential_add(APP_TLS_SEC_TAG, TLS_CREDENTIAL_CA_CERTIFICATE,
				 ca_cert, sizeof(ca_cert));
	if (ret < 0) {
		LOG_ERR("Failed to register CA certificate: %d", ret);
		return ret;
	}

#if defined(APP_HAVE_CLIENT_CERT)
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
