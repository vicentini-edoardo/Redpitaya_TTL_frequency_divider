/*
 * rp_pulse_ctl.c — Red Pitaya pulse generator register control helper
 *
 * Accesses the custom FPGA core via /dev/mem + mmap and prints all register
 * values as a single JSON object on stdout. The GUI calls this binary over SSH.
 *
 * Compile on the board:
 *   gcc -O2 -o /root/rp_pulse_ctl rp_pulse_ctl.c
 *
 * Register map (base address passed as first argument, default 0x40600000):
 *   0x00  control      bit 0 = output enable, bit 1 = soft reset
 *   0x04  divider      frequency divider value (1–32)
 *   0x08  width        pulse width in 125 MHz clock cycles
 *   0x0C  delay        pulse delay in 125 MHz clock cycles
 *   0x10  status       bit 0 = busy, bit 1 = period_valid, bit 2 = timeout
 *   0x14  raw_period   last raw measured input period (cycles)
 *   0x18  filt_period  filtered measured input period (cycles)
 *
 * Frequency from period: freq_hz = 125000000.0 / period_cycles
 */

#include <stdio.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/mman.h>
#include <unistd.h>

#define REG_CONTROL     0x00
#define REG_DIVIDER     0x04
#define REG_WIDTH       0x08
#define REG_DELAY       0x0C
#define REG_STATUS      0x10
#define REG_RAW_PERIOD  0x14
#define REG_FILT_PERIOD 0x18

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage:\n"
        "  %s <base_addr> read\n"
        "  %s <base_addr> write <divider> <width> <delay> <enable>\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, prog);
}

static uint32_t rd32(volatile uint8_t *base, off_t off) {
    return *(volatile uint32_t *)(base + off);
}

static void wr32(volatile uint8_t *base, off_t off, uint32_t val) {
    *(volatile uint32_t *)(base + off) = val;
}

/* Print all registers as a JSON object. The GUI parses this output. */
static void print_json(volatile uint8_t *base) {
    printf("{\"control\":%u,\"divider\":%u,\"width\":%u,\"delay\":%u,\"status\":%u,\"raw_period\":%u,\"filt_period\":%u}\n",
           rd32(base, REG_CONTROL),
           rd32(base, REG_DIVIDER),
           rd32(base, REG_WIDTH),
           rd32(base, REG_DELAY),
           rd32(base, REG_STATUS),
           rd32(base, REG_RAW_PERIOD),
           rd32(base, REG_FILT_PERIOD));
}

int main(int argc, char **argv) {
    int fd;
    void *map;
    volatile uint8_t *base;
    off_t phys;
    long page_size;
    off_t page_base;
    off_t page_off;

    if (argc < 3) {
        usage(argv[0]);
        return 1;
    }

    phys = (off_t)strtoull(argv[1], NULL, 0);
    page_size = sysconf(_SC_PAGESIZE);
    /* Align to page boundary required by mmap */
    page_base = phys & ~((off_t)page_size - 1);
    page_off  = phys - page_base;

    fd = open("/dev/mem", O_RDWR | O_SYNC);
    if (fd < 0) {
        perror("open");
        return 1;
    }

    map = mmap(NULL, page_size, PROT_READ | PROT_WRITE, MAP_SHARED, fd, page_base);
    if (map == MAP_FAILED) {
        perror("mmap");
        close(fd);
        return 1;
    }

    base = (volatile uint8_t *)map + page_off;

    if (strcmp(argv[2], "read") == 0) {
        print_json(base);

    } else if (strcmp(argv[2], "write") == 0) {
        uint32_t divider, width, delay, enable;
        if (argc != 7) {
            usage(argv[0]);
            munmap((void *)((uintptr_t)base - page_off), page_size);
            close(fd);
            return 1;
        }
        divider = (uint32_t)strtoul(argv[3], NULL, 0);
        width   = (uint32_t)strtoul(argv[4], NULL, 0);
        delay   = (uint32_t)strtoul(argv[5], NULL, 0);
        enable  = (uint32_t)strtoul(argv[6], NULL, 0);

        /* Disable output before changing parameters to avoid glitches */
        wr32(base, REG_CONTROL, 0);
        wr32(base, REG_DIVIDER, divider);
        wr32(base, REG_WIDTH, width);
        wr32(base, REG_DELAY, delay);
        wr32(base, REG_CONTROL, enable ? 1u : 0u);

        print_json(base);

    } else if (strcmp(argv[2], "soft_reset") == 0) {
        /* Pulse the reset bit (bit 1), then restore enable */
        wr32(base, REG_CONTROL, 0x2);
        wr32(base, REG_CONTROL, 0x1);
        print_json(base);

    } else {
        usage(argv[0]);
        munmap((void *)((uintptr_t)base - page_off), page_size);
        close(fd);
        return 1;
    }

    munmap((void *)((uintptr_t)base - page_off), page_size);
    close(fd);
    return 0;
}
