CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra

drv: driver.c bacapp.c bacapp.h
	$(CC) $(CFLAGS) -o drv driver.c bacapp.c

clean:
	rm -f drv
