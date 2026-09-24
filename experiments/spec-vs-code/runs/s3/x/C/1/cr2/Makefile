CC ?= cc
CFLAGS ?= -std=c11 -O2 -Wall -Wextra

drv: driver.c alarm.c alarm.h
	$(CC) $(CFLAGS) -o drv driver.c alarm.c -lm

clean:
	rm -f drv
