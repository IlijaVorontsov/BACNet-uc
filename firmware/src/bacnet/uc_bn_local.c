/*
 * SPDX-License-Identifier: Apache-2.0
 *
 * Local BACnet objects: creation/deletion with an owner table, property
 * access through Device_Read_Property()/Device_Write_Property() and the
 * write hook fired from the stack's WriteProperty store callback (network
 * WriteProperty/WritePropertyMultiple and local writes alike).
 *
 * The object implementations keep a pointer to the object name (zero
 * copy, bacnet_character_cstring_set()), so names live in the owner table.
 *
 * CreateObject/DeleteObject from the network: without
 * CONFIG_UC_BACNET_REMOTE_CREATE_DELETE both services are unregistered
 * (Reject unrecognized-service). With it, clients may create the supported
 * object types (owner UC_OWNER_NETWORK) and delete only those; system, io
 * and app objects answer Error object/object-deletion-not-permitted.
 */
#include <errno.h>
#include <math.h>
#include <stddef.h>
#include <string.h>

#include <zephyr/kernel.h>
#include <zephyr/logging/log.h>
#include <zephyr/spinlock.h>
#include <zephyr/sys/util.h>

#include "bacnet/bacdef.h"
#include "bacnet/apdu.h"
#include "bacnet/bacapp.h"
#include "bacnet/bacerror.h"
#include "bacnet/bacstr.h"
#include "bacnet/create_object.h"
#include "bacnet/delete_object.h"
#include "bacnet/npdu.h"
#include "bacnet/proplist.h"
#include "bacnet/rp.h"
#include "bacnet/wp.h"
#include "bacnet/basic/services.h"
#include "bacnet/basic/tsm/tsm.h"
#include "bacnet/datalink/datalink.h"
#include "bacnet/basic/object/device.h"
#include "bacnet/basic/object/ai.h"
#include "bacnet/basic/object/ao.h"
#include "bacnet/basic/object/av.h"
#include "bacnet/basic/object/bi.h"
#include "bacnet/basic/object/bo.h"
#include "bacnet/basic/object/bv.h"
#include "bacnet/basic/object/ms-input.h"
#include "bacnet/basic/object/mso.h"
#include "bacnet/basic/object/msv.h"

#include "uc/uc_bacnet.h"
#include "uc/uc_common.h"
#include "uc_bn_internal.h"

LOG_MODULE_REGISTER(uc_bn_local, CONFIG_UC_LOG_LEVEL);

/* ---------------------------------------------------------------------- */
/* Supported object types                                                  */
/* ---------------------------------------------------------------------- */

typedef bool (*name_set_fn)(uint32_t instance, const char *name);

struct obj_ops {
	uint16_t type;
	name_set_fn name_set;
};

static const struct obj_ops obj_ops_table[] = {
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_INPUT)
	{ OBJECT_ANALOG_INPUT, Analog_Input_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_OUTPUT)
	{ OBJECT_ANALOG_OUTPUT, Analog_Output_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_VALUE)
	{ OBJECT_ANALOG_VALUE, Analog_Value_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_INPUT)
	{ OBJECT_BINARY_INPUT, Binary_Input_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_OUTPUT)
	{ OBJECT_BINARY_OUTPUT, Binary_Output_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_VALUE)
	{ OBJECT_BINARY_VALUE, Binary_Value_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_INPUT)
	{ OBJECT_MULTI_STATE_INPUT, Multistate_Input_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_OUTPUT)
	{ OBJECT_MULTI_STATE_OUTPUT, Multistate_Output_Name_Set },
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_VALUE)
	{ OBJECT_MULTI_STATE_VALUE, Multistate_Value_Name_Set },
#endif
};

static const struct obj_ops *obj_ops_find(uint16_t type)
{
	for (size_t i = 0; i < ARRAY_SIZE(obj_ops_table); i++) {
		if (obj_ops_table[i].type == type) {
			return &obj_ops_table[i];
		}
	}

	return NULL;
}

static bool obj_type_is_system(uint16_t type)
{
	return (type == OBJECT_DEVICE) || (type == OBJECT_NETWORK_PORT);
}

/* ---------------------------------------------------------------------- */
/* Owner table                                                             */
/* ---------------------------------------------------------------------- */

struct owner_entry {
	bool used;
	uint8_t owner;
	uint16_t type;
	uint32_t instance;
	char name[UC_NAME_MAX];
};

/* Modified in the BACnet thread only; read from any thread. */
static struct owner_entry owners[CONFIG_UC_BACNET_OBJECTS_MAX];
static struct k_spinlock owner_lock;

static struct owner_entry *owner_find(uint16_t type, uint32_t instance)
{
	for (size_t i = 0; i < ARRAY_SIZE(owners); i++) {
		if (owners[i].used && (owners[i].type == type) &&
		    (owners[i].instance == instance)) {
			return &owners[i];
		}
	}

	return NULL;
}

static uint8_t owner_get(uint16_t type, uint32_t instance)
{
	struct owner_entry *e;
	k_spinlock_key_t key;
	uint8_t owner = UC_OWNER_NONE;

	if (obj_type_is_system(type)) {
		return UC_OWNER_SYSTEM;
	}
	key = k_spin_lock(&owner_lock);
	e = owner_find(type, instance);
	if (e != NULL) {
		owner = e->owner;
	}
	k_spin_unlock(&owner_lock, key);

	return owner;
}

static void owner_free(struct owner_entry *e)
{
	k_spinlock_key_t key = k_spin_lock(&owner_lock);

	memset(e, 0, sizeof(*e));
	k_spin_unlock(&owner_lock, key);
}

/* Take a free entry for (type, instance). An entry left for the same
 * object id is dropped first: callers only allocate for objects that do
 * not exist. NULL when the table is full. BACnet thread only. */
static struct owner_entry *owner_alloc_locked(uint16_t type, uint32_t instance, uint8_t owner)
{
	struct owner_entry *e = owner_find(type, instance);
	k_spinlock_key_t key;

	if (e != NULL) {
		owner_free(e);
		e = NULL;
	}
	key = k_spin_lock(&owner_lock);
	for (size_t i = 0; i < ARRAY_SIZE(owners); i++) {
		if (!owners[i].used) {
			e = &owners[i];
			e->used = true;
			e->owner = owner;
			e->type = type;
			e->instance = instance;
			e->name[0] = '\0';
			break;
		}
	}
	k_spin_unlock(&owner_lock, key);

	return e;
}

/* Owner of an existing object; drops entries of objects that no longer
 * exist. BACnet thread only. */
static uint8_t owner_of_locked(uint16_t type, uint32_t instance)
{
	struct owner_entry *e;

	if (obj_type_is_system(type)) {
		return UC_OWNER_SYSTEM;
	}
	e = owner_find(type, instance);
	if (e == NULL) {
		return UC_OWNER_NONE;
	}
	if (!Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance)) {
		owner_free(e);
		return UC_OWNER_NONE;
	}

	return e->owner;
}

uint8_t uc_bn_obj_owner(uint16_t type, uint32_t instance)
{
	if (uc_bn_in_thread()) {
		return owner_of_locked(type, instance);
	}

	return owner_get(type, instance);
}

/* ---------------------------------------------------------------------- */
/* Error mapping                                                           */
/* ---------------------------------------------------------------------- */

int uc_bn_local_err(BACNET_ERROR_CLASS error_class, BACNET_ERROR_CODE error_code)
{
	ARG_UNUSED(error_class);

	switch (error_code) {
	case ERROR_CODE_UNKNOWN_OBJECT:
	case ERROR_CODE_UNKNOWN_PROPERTY:
		return -ENOENT;
	case ERROR_CODE_WRITE_ACCESS_DENIED:
	case ERROR_CODE_READ_ACCESS_DENIED:
	case ERROR_CODE_OBJECT_DELETION_NOT_PERMITTED:
	case ERROR_CODE_DYNAMIC_CREATION_NOT_SUPPORTED:
		return -EACCES;
	case ERROR_CODE_INVALID_DATA_TYPE:
		return -EBADMSG;
	case ERROR_CODE_OBJECT_IDENTIFIER_ALREADY_EXISTS:
	case ERROR_CODE_DUPLICATE_NAME:
		return -EEXIST;
	case ERROR_CODE_NO_SPACE_FOR_OBJECT:
	case ERROR_CODE_NO_SPACE_TO_WRITE_PROPERTY:
		return -ENOMEM;
	case ERROR_CODE_UNSUPPORTED_OBJECT_TYPE:
		return -ENOTSUP;
	default:
		return -EINVAL;
	}
}

/* ---------------------------------------------------------------------- */
/* Write hook (WriteProperty store callback)                               */
/* ---------------------------------------------------------------------- */

static uc_bn_write_hook_t write_hook;
/* Set while uc_bn_prop_write_locked() runs Device_Write_Property(). */
static bool local_write_active;
static uint8_t local_writer;

void uc_bn_set_write_hook(uc_bn_write_hook_t hook)
{
	write_hook = hook;
}

/* Called by Device_Write_Property() after every successful write: network
 * WriteProperty, WritePropertyMultiple and uc_bn_prop_write_locked(). */
static bool bn_store_callback(BACNET_WRITE_PROPERTY_DATA *wp)
{
	uc_bn_write_hook_t hook = write_hook;
	BACNET_APPLICATION_DATA_VALUE value;
	uint8_t owner;
	uint8_t writer;
	int len;

	if ((hook == NULL) || (wp == NULL)) {
		return true;
	}
	writer = local_write_active ? local_writer : UC_OWNER_NONE;
	owner = owner_of_locked((uint16_t)wp->object_type, wp->object_instance);
	if ((writer != UC_OWNER_NONE) && (writer == owner)) {
		/* an owner writing its own object gets no echo */
		return true;
	}
	memset(&value, 0, sizeof(value));
	len = bacapp_decode_application_data(wp->application_data,
					     (uint32_t)MAX(wp->application_data_len, 0), &value);
	if (len <= 0) {
		LOG_DBG("write %u:%u prop %u not decodable", wp->object_type, wp->object_instance,
			wp->object_property);
		return true;
	}
	hook(owner, writer, (uint16_t)wp->object_type, wp->object_instance,
	     (uint32_t)wp->object_property, wp->priority, &value);

	return true;
}

static void net_services_init_locked(void);

void uc_bn_local_init_locked(void)
{
	k_spinlock_key_t key = k_spin_lock(&owner_lock);

	memset(owners, 0, sizeof(owners));
	k_spin_unlock(&owner_lock, key);
	/* Replaces the store callback of bacnet_basic_init() (which only
	 * forwards to bacnet_basic_store_callback_set(); not used here). */
	Device_Write_Property_Store_Callback_Set(bn_store_callback);
	net_services_init_locked();
}

/* ---------------------------------------------------------------------- */
/* Create / delete                                                         */
/* ---------------------------------------------------------------------- */

/* Scratch (BACnet thread only); contains a MAX_APDU buffer. */
static BACNET_CREATE_OBJECT_DATA create_data;

static void name_uniqueness_check(const char *name, uint16_t type, uint32_t instance)
{
	BACNET_CHARACTER_STRING cs;
	BACNET_OBJECT_TYPE other_type;
	uint32_t other_instance;

	if (!characterstring_init_ansi(&cs, name)) {
		return;
	}
	if (Device_Valid_Object_Name(&cs, &other_type, &other_instance) &&
	    ((other_type != type) || (other_instance != instance))) {
		LOG_WRN("object name '%s' also used by %u:%u", name, other_type, other_instance);
	}
}

int uc_bn_obj_create_locked(uint16_t type, uint32_t instance, const char *name, uint8_t owner)
{
	const struct obj_ops *ops = obj_ops_find(type);
	struct owner_entry *e;

	if ((ops == NULL) || (instance >= BACNET_MAX_INSTANCE)) {
		return -EINVAL;
	}
	if ((name != NULL) && (strlen(name) >= UC_NAME_MAX)) {
		return -EINVAL;
	}

	if (Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance)) {
		return (owner_of_locked(type, instance) == owner) ? 0 : -EEXIST;
	}

	e = owner_alloc_locked(type, instance, owner);
	if (e == NULL) {
		return -ENOSPC;
	}

	memset(&create_data, 0, sizeof(create_data));
	create_data.object_type = (BACNET_OBJECT_TYPE)type;
	create_data.object_instance = instance;
	if (!Device_Create_Object(&create_data) ||
	    !Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance)) {
		int err = uc_bn_local_err(create_data.error_class, create_data.error_code);

		owner_free(e);
		LOG_ERR("create %u:%u failed (%d)", type, instance, err);
		return err;
	}

	if ((name != NULL) && (name[0] != '\0')) {
		memcpy(e->name, name, strlen(name) + 1);
		name_uniqueness_check(e->name, type, instance);
		(void)ops->name_set(instance, e->name);
	}
	LOG_DBG("created %u:%u owner %u", type, instance, owner);

	return 0;
}

int uc_bn_obj_delete_locked(uint16_t type, uint32_t instance, uint8_t owner)
{
	BACNET_DELETE_OBJECT_DATA data = { 0 };
	struct owner_entry *e;
	uint8_t current;

	if (obj_type_is_system(type)) {
		return -EACCES;
	}
	e = owner_find(type, instance);
	if (!Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance)) {
		if (e != NULL) {
			owner_free(e);
		}
		return -ENOENT;
	}
	current = (e != NULL) ? e->owner : UC_OWNER_NONE;
	if (current == UC_OWNER_SYSTEM) {
		return -EACCES;
	}
	if ((owner != UC_OWNER_NONE) && (owner != current)) {
		return -EACCES;
	}

	data.object_type = (BACNET_OBJECT_TYPE)type;
	data.object_instance = instance;
	if (!Device_Delete_Object(&data)) {
		return uc_bn_local_err(data.error_class, data.error_code);
	}
	if (e != NULL) {
		owner_free(e);
	}
	LOG_DBG("deleted %u:%u", type, instance);

	return 0;
}

struct obj_args {
	uint16_t type;
	uint32_t instance;
	const char *name;
	uint8_t owner;
	int rc;
};

static void obj_create_fn(void *arg)
{
	struct obj_args *a = arg;

	a->rc = uc_bn_obj_create_locked(a->type, a->instance, a->name, a->owner);
}

int uc_bn_obj_create(uint16_t type, uint32_t instance, const char *name, uint8_t owner)
{
	struct obj_args a = {
		.type = type, .instance = instance, .name = name, .owner = owner, .rc = -EIO,
	};
	int rc = uc_bn_call(obj_create_fn, &a);

	return (rc != 0) ? rc : a.rc;
}

static void obj_delete_fn(void *arg)
{
	struct obj_args *a = arg;

	a->rc = uc_bn_obj_delete_locked(a->type, a->instance, a->owner);
}

int uc_bn_obj_delete(uint16_t type, uint32_t instance, uint8_t owner)
{
	struct obj_args a = { .type = type, .instance = instance, .owner = owner, .rc = -EIO };
	int rc = uc_bn_call(obj_delete_fn, &a);

	return (rc != 0) ? rc : a.rc;
}

static void obj_delete_owned_fn(void *arg)
{
	struct obj_args *a = arg;
	int count = 0;

	for (size_t i = 0; i < ARRAY_SIZE(owners); i++) {
		struct owner_entry *e = &owners[i];

		if (!e->used || (e->owner != a->owner)) {
			continue;
		}
		if (uc_bn_obj_delete_locked(e->type, e->instance, a->owner) == 0) {
			count++;
		} else if (e->used &&
			   Device_Valid_Object_Id((BACNET_OBJECT_TYPE)e->type, e->instance)) {
			/* keep the entry: the object references its name */
			LOG_WRN("%u:%u of owner %u not deleted", e->type, e->instance, e->owner);
		} else if (e->used) {
			/* the object vanished: forget it */
			owner_free(e);
		}
	}
	a->rc = count;
}

int uc_bn_obj_delete_owned(uint8_t owner)
{
	struct obj_args a = { .owner = owner, .rc = -EIO };
	int rc;

	if (owner == UC_OWNER_SYSTEM) {
		return -EACCES;
	}
	rc = uc_bn_call(obj_delete_owned_fn, &a);

	return (rc != 0) ? rc : a.rc;
}

/* ---------------------------------------------------------------------- */
/* Network CreateObject / DeleteObject                                     */
/* ---------------------------------------------------------------------- */

/* Encode the NPDU of a reply to src into Handler_Transmit_Buffer. */
static int reply_npdu_encode(BACNET_ADDRESS *src, uint8_t priority, BACNET_NPDU_DATA *npdu_data)
{
	BACNET_ADDRESS my_address;

	datalink_get_my_address(&my_address);
	npdu_encode_npdu_data(npdu_data, false, (BACNET_MESSAGE_PRIORITY)priority);

	return npdu_encode_pdu(&Handler_Transmit_Buffer[0], src, &my_address, npdu_data);
}

static void reply_send(BACNET_ADDRESS *src, BACNET_NPDU_DATA *npdu_data, int pdu_len)
{
	if (datalink_send_pdu(src, npdu_data, &Handler_Transmit_Buffer[0], (unsigned int)pdu_len) <=
	    0) {
		LOG_DBG("reply not sent");
	}
}

void uc_bn_reply_error(BACNET_ADDRESS *src, const BACNET_CONFIRMED_SERVICE_DATA *service_data,
		       BACNET_CONFIRMED_SERVICE service, BACNET_ERROR_CLASS error_class,
		       BACNET_ERROR_CODE error_code)
{
	BACNET_NPDU_DATA npdu_data;
	int len = reply_npdu_encode(src, service_data->priority, &npdu_data);

	len += bacerror_encode_apdu(&Handler_Transmit_Buffer[len], service_data->invoke_id,
				    service, error_class, error_code);
	reply_send(src, &npdu_data, len);
}

#if defined(CONFIG_UC_BACNET_REMOTE_CREATE_DELETE)
/* CreateObject: like handler_create_object(), but only for the object
 * types of obj_ops_table and with an owner entry (UC_OWNER_NETWORK) for
 * every created object. Malformed or segmented requests are left to the
 * stack's handler (Reject/Abort). */
static void net_create_object_handler(uint8_t *service_request, uint16_t service_len,
				      BACNET_ADDRESS *src,
				      BACNET_CONFIRMED_SERVICE_DATA *service_data)
{
	BACNET_CREATE_OBJECT_DATA *data = &create_data;
	BACNET_NPDU_DATA npdu_data;
	struct owner_entry *e = NULL;
	k_spinlock_key_t key;
	uint16_t type;
	int len;

	memset(data, 0, sizeof(*data));
	if ((service_len == 0) || service_data->segmented_message ||
	    (create_object_decode_service_request(service_request, service_len, data) <= 0)) {
		handler_create_object(service_request, service_len, src, service_data);
		return;
	}
	type = (uint16_t)data->object_type;
	if (obj_ops_find(type) == NULL) {
		data->error_class = ERROR_CLASS_OBJECT;
		data->error_code = ERROR_CODE_DYNAMIC_CREATION_NOT_SUPPORTED;
	} else if (Device_Valid_Object_Id(data->object_type, data->object_instance)) {
		data->error_class = ERROR_CLASS_OBJECT;
		data->error_code = ERROR_CODE_OBJECT_IDENTIFIER_ALREADY_EXISTS;
	} else {
		/* the instance of a CreateObject by type is known afterwards */
		e = owner_alloc_locked(type, data->object_instance, UC_OWNER_NETWORK);
		if (e == NULL) {
			data->error_class = ERROR_CLASS_RESOURCES;
			data->error_code = ERROR_CODE_NO_SPACE_FOR_OBJECT;
		}
	}

	len = reply_npdu_encode(src, service_data->priority, &npdu_data);
	if ((e != NULL) && Device_Create_Object(data) &&
	    Device_Valid_Object_Id(data->object_type, data->object_instance)) {
		/* drop a left-over entry of a former object with this id */
		for (size_t i = 0; i < ARRAY_SIZE(owners); i++) {
			if ((&owners[i] != e) && owners[i].used && (owners[i].type == type) &&
			    (owners[i].instance == data->object_instance)) {
				owner_free(&owners[i]);
			}
		}
		key = k_spin_lock(&owner_lock);
		e->instance = data->object_instance;
		k_spin_unlock(&owner_lock, key);
		LOG_INF("CreateObject %s %u from the network", uc_obj_type_to_str(type),
			data->object_instance);
		len += create_object_ack_encode(&Handler_Transmit_Buffer[len],
						service_data->invoke_id, data);
	} else {
		if (e != NULL) {
			owner_free(e);
		}
		LOG_WRN("CreateObject %s %u from the network refused (%u/%u)",
			uc_obj_type_to_str(type), data->object_instance, data->error_class,
			data->error_code);
		len += create_object_error_ack_encode(&Handler_Transmit_Buffer[len],
						      service_data->invoke_id, data);
	}
	reply_send(src, &npdu_data, len);
}

/* DeleteObject: only objects created with CreateObject (UC_OWNER_NETWORK);
 * the owner table entry is released with the object. */
static void net_delete_object_handler(uint8_t *service_request, uint16_t service_len,
				      BACNET_ADDRESS *src,
				      BACNET_CONFIRMED_SERVICE_DATA *service_data)
{
	BACNET_DELETE_OBJECT_DATA data = { 0 };
	struct owner_entry *e;
	uint16_t type;
	uint8_t owner;

	if ((service_len == 0) || service_data->segmented_message ||
	    (delete_object_decode_service_request(service_request, service_len, &data) <= 0) ||
	    !Device_Valid_Object_Id(data.object_type, data.object_instance)) {
		/* Reject/Abort, or Error unknown-object/unsupported-object-type */
		handler_delete_object(service_request, service_len, src, service_data);
		return;
	}
	type = (uint16_t)data.object_type;
	owner = owner_of_locked(type, data.object_instance);
	if (owner != UC_OWNER_NETWORK) {
		LOG_WRN("DeleteObject %s %u from the network refused (owner %u)",
			uc_obj_type_to_str(type), data.object_instance, owner);
		uc_bn_reply_error(src, service_data, SERVICE_CONFIRMED_DELETE_OBJECT,
				  ERROR_CLASS_OBJECT, ERROR_CODE_OBJECT_DELETION_NOT_PERMITTED);
		return;
	}
	handler_delete_object(service_request, service_len, src, service_data);
	e = owner_find(type, data.object_instance);
	if ((e != NULL) && !Device_Valid_Object_Id(data.object_type, data.object_instance)) {
		owner_free(e);
		LOG_INF("DeleteObject %s %u from the network", uc_obj_type_to_str(type),
			data.object_instance);
	}
}
#endif /* CONFIG_UC_BACNET_REMOTE_CREATE_DELETE */

/* Replace the stack's CreateObject/DeleteObject handlers registered by
 * bacnet_basic_init(). Without a handler apdu_handler() answers Reject
 * unrecognized-service, and Protocol_Services_Supported omits them. */
static void net_services_init_locked(void)
{
#if defined(CONFIG_UC_BACNET_REMOTE_CREATE_DELETE)
	apdu_set_confirmed_handler(SERVICE_CONFIRMED_CREATE_OBJECT, net_create_object_handler);
	apdu_set_confirmed_handler(SERVICE_CONFIRMED_DELETE_OBJECT, net_delete_object_handler);
#else
	apdu_set_confirmed_handler(SERVICE_CONFIRMED_CREATE_OBJECT, NULL);
	apdu_set_confirmed_handler(SERVICE_CONFIRMED_DELETE_OBJECT, NULL);
#endif
}

/* ---------------------------------------------------------------------- */
/* Property access                                                         */
/* ---------------------------------------------------------------------- */

/* Scratch buffers (BACnet thread only). */
static uint8_t rp_buf[MAX_APDU];
static BACNET_WRITE_PROPERTY_DATA wp_data;

/* An array index is only valid for BACnetARRAY properties (like the
 * ReadProperty/WriteProperty handlers: property-is-not-an-array). */
static bool prop_index_ok(uint16_t type, uint32_t prop, int32_t index)
{
	return (index < 0) || property_list_bacnet_array_member((BACNET_OBJECT_TYPE)type,
								 (BACNET_PROPERTY_ID)prop);
}

/* Device_Read_Property() into rp_buf: the encoded length (0 for an empty
 * array or list) or a negative errno. */
static int prop_read_raw_locked(uint16_t type, uint32_t instance, uint32_t prop, int32_t index)
{
	BACNET_READ_PROPERTY_DATA rp = { 0 };
	int len;

	if (!prop_index_ok(type, prop, index)) {
		return -EINVAL;
	}
	rp.object_type = (BACNET_OBJECT_TYPE)type;
	rp.object_instance = instance;
	rp.object_property = (BACNET_PROPERTY_ID)prop;
	rp.array_index = (index < 0) ? BACNET_ARRAY_ALL : (BACNET_ARRAY_INDEX)index;
	rp.application_data = rp_buf;
	rp.application_data_len = sizeof(rp_buf);

	len = Device_Read_Property(&rp);
	if (len < 0) {
		if (len == BACNET_STATUS_ERROR) {
			return uc_bn_local_err(rp.error_class, rp.error_code);
		}
		/* abort (value larger than one APDU) or reject */
		return (len == BACNET_STATUS_ABORT) ? -ENOSPC : -EINVAL;
	}

	return len;
}

int uc_bn_prop_read_locked(uint16_t type, uint32_t instance, uint32_t prop, int32_t index,
			   BACNET_APPLICATION_DATA_VALUE *out)
{
	int len;

	if (out == NULL) {
		return -EINVAL;
	}
	len = prop_read_raw_locked(type, instance, prop, index);
	if (len < 0) {
		return len;
	}
	if ((len == 0) || (bacapp_decode_application_data(rp_buf, (uint32_t)len, out) <= 0)) {
		return -EBADMSG;
	}
	out->next = NULL;

	return 0;
}

static int prop_read_encoded_locked(uint16_t type, uint32_t instance, uint32_t prop,
				    int32_t index, uint8_t *buf, size_t size)
{
	int len = prop_read_raw_locked(type, instance, prop, index);

	if (len < 0) {
		return len;
	}
	if ((size_t)len > size) {
		return -ENOSPC;
	}
	memcpy(buf, rp_buf, (size_t)len);

	return len;
}

int uc_bn_prop_write_locked(uint16_t type, uint32_t instance, uint32_t prop, int32_t index,
			    const BACNET_APPLICATION_DATA_VALUE *value, uint8_t priority,
			    uint8_t writer_owner)
{
	BACNET_APPLICATION_DATA_VALUE null_value = { 0 };
	int len;
	bool ok;

	if (priority > BACNET_MAX_PRIORITY) {
		return -EINVAL;
	}
	if (local_write_active) {
		/* wp_data is in use by an outer local write (hook recursion) */
		return -EBUSY;
	}
	if (value == NULL) {
		null_value.tag = BACNET_APPLICATION_TAG_NULL;
		value = &null_value;
	}
	if (value->context_specific || !prop_index_ok(type, prop, index)) {
		return -EINVAL;
	}

	memset(&wp_data, 0, sizeof(wp_data));
	wp_data.object_type = (BACNET_OBJECT_TYPE)type;
	wp_data.object_instance = instance;
	wp_data.object_property = (BACNET_PROPERTY_ID)prop;
	wp_data.array_index = (index < 0) ? BACNET_ARRAY_ALL : (BACNET_ARRAY_INDEX)index;
	/* like a WriteProperty request without priority: 16 */
	wp_data.priority = (priority == 0) ? BACNET_MAX_PRIORITY : priority;
	len = bacapp_encode_application_data(wp_data.application_data, value);
	if (len <= 0) {
		return -EBADMSG;
	}
	wp_data.application_data_len = len;

#if (BACNET_PROTOCOL_REVISION >= 21)
	/* Like handler_write_property() (135 clause 15.9.2): a NULL written to
	 * the present value of an object without priority array (AV, BV, MSV
	 * of this stack) changes nothing and succeeds. Unlike the handler, only
	 * for an existing object. */
	if (Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance) &&
	    write_property_relinquish_bypass(&wp_data, Device_Objects_Property_List_Member)) {
		return 0;
	}
#endif

	local_writer = writer_owner;
	local_write_active = true;
	ok = Device_Write_Property(&wp_data);
	local_write_active = false;

	if (!ok) {
		return uc_bn_local_err(wp_data.error_class, wp_data.error_code);
	}

	return 0;
}

struct prop_args {
	uint16_t type;
	uint32_t instance;
	uint32_t prop;
	int32_t index;
	BACNET_APPLICATION_DATA_VALUE *out;
	const BACNET_APPLICATION_DATA_VALUE *value;
	uint8_t *buf;
	size_t size;
	uint8_t priority;
	uint8_t writer;
	int rc;
};

static void prop_read_fn(void *arg)
{
	struct prop_args *a = arg;

	a->rc = uc_bn_prop_read_locked(a->type, a->instance, a->prop, a->index, a->out);
}

int uc_bn_prop_read(uint16_t type, uint32_t instance, uint32_t prop, int32_t index,
		    BACNET_APPLICATION_DATA_VALUE *out)
{
	struct prop_args a = {
		.type = type, .instance = instance, .prop = prop, .index = index, .out = out,
		.rc = -EIO,
	};
	int rc;

	if (out == NULL) {
		return -EINVAL;
	}
	rc = uc_bn_call(prop_read_fn, &a);

	return (rc != 0) ? rc : a.rc;
}

static void prop_read_encoded_fn(void *arg)
{
	struct prop_args *a = arg;

	a->rc = prop_read_encoded_locked(a->type, a->instance, a->prop, a->index, a->buf,
					 a->size);
}

int uc_bn_prop_read_encoded(uint16_t type, uint32_t instance, uint32_t prop, int32_t index,
			    uint8_t *buf, size_t size)
{
	struct prop_args a = {
		.type = type, .instance = instance, .prop = prop, .index = index, .buf = buf,
		.size = size, .rc = -EIO,
	};
	int rc;

	if ((buf == NULL) || (size == 0)) {
		return -EINVAL;
	}
	rc = uc_bn_call(prop_read_encoded_fn, &a);

	return (rc != 0) ? rc : a.rc;
}

static void prop_write_fn(void *arg)
{
	struct prop_args *a = arg;

	a->rc = uc_bn_prop_write_locked(a->type, a->instance, a->prop, a->index, a->value,
					a->priority, a->writer);
}

int uc_bn_prop_write(uint16_t type, uint32_t instance, uint32_t prop, int32_t index,
		     const BACNET_APPLICATION_DATA_VALUE *value, uint8_t priority,
		     uint8_t writer_owner)
{
	struct prop_args a = {
		.type = type, .instance = instance, .prop = prop, .index = index,
		.value = value, .priority = priority, .writer = writer_owner, .rc = -EIO,
	};
	int rc = uc_bn_call(prop_write_fn, &a);

	return (rc != 0) ? rc : a.rc;
}

/* ---------------------------------------------------------------------- */
/* Type-specific setters                                                   */
/* ---------------------------------------------------------------------- */

int uc_bn_input_pv_set_locked(uint16_t type, uint32_t instance, double value)
{
	if (isnan(value)) {
		return -EINVAL;
	}
	if ((type != OBJECT_ANALOG_INPUT) && (type != OBJECT_BINARY_INPUT) &&
	    (type != OBJECT_MULTI_STATE_INPUT)) {
		return -EINVAL;
	}
	if (!Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance)) {
		return -ENOENT;
	}

	/* Out_Of_Service decouples Present_Value from the physical input: a
	 * client may then write it (test/override); the scan must not. */
	switch (type) {
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_INPUT)
	case OBJECT_ANALOG_INPUT:
		if (!Analog_Input_Out_Of_Service(instance)) {
			Analog_Input_Present_Value_Set(instance, (float)value);
		}
		return 0;
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_BINARY_INPUT)
	case OBJECT_BINARY_INPUT:
		if (Binary_Input_Out_Of_Service(instance)) {
			return 0;
		}
		return Binary_Input_Present_Value_Set(instance, (value != 0.0) ? BINARY_ACTIVE
									   : BINARY_INACTIVE)
			       ? 0
			       : -EINVAL;
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_MULTISTATE_INPUT)
	case OBJECT_MULTI_STATE_INPUT:
		if ((value < 1.0) || (value > (double)UINT32_MAX)) {
			return -EINVAL;
		}
		if (Multistate_Input_Out_Of_Service(instance)) {
			return 0;
		}
		return Multistate_Input_Present_Value_Set(instance, (uint32_t)value) ? 0 : -EINVAL;
#endif
	default:
		return -EINVAL;
	}
}

int uc_bn_analog_setup_locked(uint16_t type, uint32_t instance, uint16_t units,
			      double cov_increment)
{
	BACNET_ENGINEERING_UNITS u = (BACNET_ENGINEERING_UNITS)units;
	float inc = (float)cov_increment;

	if (isnan(cov_increment) || (cov_increment < 0.0)) {
		return -EINVAL;
	}
	if (!Device_Valid_Object_Id((BACNET_OBJECT_TYPE)type, instance)) {
		return -ENOENT;
	}

	switch (type) {
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_INPUT)
	case OBJECT_ANALOG_INPUT:
		if (!Analog_Input_Units_Set(instance, u)) {
			return -EINVAL;
		}
		Analog_Input_COV_Increment_Set(instance, inc);
		return 0;
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_OUTPUT)
	case OBJECT_ANALOG_OUTPUT:
		if (!Analog_Output_Units_Set(instance, u)) {
			return -EINVAL;
		}
		Analog_Output_COV_Increment_Set(instance, inc);
		return 0;
#endif
#if defined(CONFIG_BACNET_BASIC_OBJECT_ANALOG_VALUE)
	case OBJECT_ANALOG_VALUE:
		if (!Analog_Value_Units_Set(instance, u)) {
			return -EINVAL;
		}
		Analog_Value_COV_Increment_Set(instance, inc);
		return 0;
#endif
	default:
		return -EINVAL;
	}
}

/* ---------------------------------------------------------------------- */
/* Object list                                                             */
/* ---------------------------------------------------------------------- */

struct list_args {
	size_t offset;
	struct uc_bn_obj_info *out;
	size_t max;
	size_t total;
	int rc;
};

static void obj_list_fn(void *arg)
{
	struct list_args *a = arg;
	BACNET_CHARACTER_STRING cs;
	unsigned int total = Device_Object_List_Count();
	size_t n = 0;

	for (size_t i = a->offset; (i < total) && (n < a->max); i++) {
		struct uc_bn_obj_info *info = &a->out[n];
		BACNET_OBJECT_TYPE type;
		uint32_t instance;

		if (!Device_Object_List_Identifier((uint32_t)(i + 1), &type, &instance)) {
			break;
		}
		memset(info, 0, sizeof(*info));
		info->type = (uint16_t)type;
		info->instance = instance;
		if (Device_Object_Name_Copy(type, instance, &cs)) {
			(void)characterstring_ansi_copy(info->name, sizeof(info->name), &cs);
		}
		info->owner = owner_of_locked((uint16_t)type, instance);
		info->has_pv = (uc_bn_prop_read_locked((uint16_t)type, instance, PROP_PRESENT_VALUE,
						       -1, &info->pv) == 0);
		n++;
	}
	a->total = total;
	a->rc = (int)n;
}

int uc_bn_obj_list(size_t offset, struct uc_bn_obj_info *out, size_t max, size_t *total)
{
	struct list_args a = { .offset = offset, .out = out, .max = max, .rc = -EIO };
	int rc;

	if ((out == NULL) && (max > 0)) {
		return -EINVAL;
	}
	rc = uc_bn_call(obj_list_fn, &a);
	if (rc != 0) {
		return rc;
	}
	if (total != NULL) {
		*total = a.total;
	}

	return a.rc;
}
