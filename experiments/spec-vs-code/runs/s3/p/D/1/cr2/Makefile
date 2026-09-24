CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra

drv: driver.c bacapp.c bacapp.h
	$(CC) $(CFLAGS) -o drv driver.c bacapp.c

# CR-102: bacapp.c must not need the soft-float support routines on the
# FPU-less Cortex-M0. Cross-compiles it and fails if the object references
# any of them (AEABI or generic libgcc names). Needs an ARM cross toolchain.
M0CC ?= arm-none-eabi-gcc
M0CFLAGS ?= -mcpu=cortex-m0 -mthumb -mfloat-abi=soft -std=c11 -Os -Wall -Wextra
M0NM ?= arm-none-eabi-nm
SOFTFLOAT_RE = __aeabi_(c?[fd]|[a-z]*2[fd])|__[a-z]+[sd]f

check-m0: bacapp.c bacapp.h
	$(M0CC) $(M0CFLAGS) -c -o bacapp-m0.o bacapp.c
	@if $(M0NM) -u bacapp-m0.o | grep -E '$(SOFTFLOAT_RE)'; then \
		echo "check-m0: FAIL - bacapp.c references soft-float routines (CR-102)"; \
		rm -f bacapp-m0.o; exit 1; \
	fi
	@rm -f bacapp-m0.o
	@echo "check-m0: OK - no soft-float references"

clean:
	rm -f drv bacapp-m0.o

.PHONY: check-m0 clean
