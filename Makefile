CC      = g++
CFLAGS  = -O2 -Wall -std=c++20 -I/boot/include
LIBS    = -L/boot/lib -Wl,-rpath,/boot/lib -lrp -lm -lpthread
TARGET  = rp_pll

all: $(TARGET) gen_pwm

$(TARGET): rp_pll.c
	$(CC) $(CFLAGS) -o $(TARGET) rp_pll.c $(LIBS) -lrp-hw-calib

gen_pwm: gen_pwm.cpp
	$(CC) $(CFLAGS) -o gen_pwm gen_pwm.cpp $(LIBS) -lrp-hw-calib

clean:
	rm -f $(TARGET) gen_pwm

.PHONY: all clean
