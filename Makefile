CC      = gcc
CFLAGS  = -O2 -Wall -I/opt/redpitaya/include
LIBS    = -lrp -lm -lpthread
TARGET  = rp_pll

all: $(TARGET)

$(TARGET): rp_pll.c
	$(CC) $(CFLAGS) -o $(TARGET) rp_pll.c $(LIBS)

clean:
	rm -f $(TARGET)

.PHONY: all clean
