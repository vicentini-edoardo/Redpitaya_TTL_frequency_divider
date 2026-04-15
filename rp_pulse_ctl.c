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
 *   0x00  control      bit 0 = output enable, bit 1 = soft reset, bit 2 = phase modulation enable
 *   0x04  divider      frequency divider value (1–32)
 *   0x08  width        pulse width in 125 MHz clock cycles
 *   0x0C  delay        pulse delay in 125 MHz clock cycles
 *   0x10  status       bit 0 = busy, bit 1 = period_valid, bit 2 = timeout, bit 3 = period_stable
 *   0x14  period       last raw measured input period (cycles)
 *   0x18  period_avg   filtered measured input period (cycles)
 *   0x1C  phase_freq   DDS phase increment word
 *   0x20  phase_amp    Q15 sweep amplitude word
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
#define REG_PHASE_FREQ  0x1C
#define REG_PHASE_AMP   0x20

static void usage(const char *prog) {
    fprintf(stderr,
        "Usage:\n"
        "  %s <base_addr> read\n"
        "  %s <base_addr> write <divider> <width> <delay> <phase_freq> <phase_amp_q15> <control>\n"
        "  %s <base_addr> write <divider> <width> <delay> <phase_freq> <control>\n"
        "  %s <base_addr> write <divider> <width> <delay> <enable>\n"
        "  %s <base_addr> soft_reset\n",
        prog, prog, prog, prog, prog);
}

static uint32_t rd32(volatile uint8_t *base, off_t off) {
    return *(volatile uint32_t *)(base + off);
}

static void wr32(volatile uint8_t *base, off_t off, uint32_t val) {
    *(volatile uint32_t *)(base + off) = val;
}

/* Print all registers as a JSON object. The GUI parses this output. */
static void print_json(volatile uint8_t *base) {
    const uint32_t raw_period  = rd32(base, REG_RAW_PERIOD);
    const uint32_t filt_period = rd32(base, REG_FILT_PERIOD);
    const uint32_t status      = rd32(base, REG_STATUS);
    /* Decode individual status bits for easier parsing in the GUI */
    const uint32_t period_stable = (status >> 3) & 0x1u;
    printf("{\"control\":%u,\"divider\":%u,\"width\":%u,\"delay\":%u,"
           "\"status\":%u,\"period_stable\":%u,"
           "\"raw_period\":%u,\"period_avg\":%u,"
           "\"phase_freq\":%u,\"phase_amp_q15\":%u}\n",
           rd32(base, REG_CONTROL),
           rd32(base, REG_DIVIDER),
           rd32(base, REG_WIDTH),
           rd32(base, REG_DELAY),
           status,
           period_stable,
           raw_period,
           filt_period,
           rd32(base, REG_PHASE_FREQ),
           rd32(base, REG_PHASE_AMP));
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
        uint32_t divider, width, delay, phase_freq, phase_amp_q15, control;
        if (argc != 7 && argc != 8 && argc != 9) {
            usage(argv[0]);
            munmap((void *)((uintptr_t)base - page_off), page_size);
            close(fd);
            return 1;
        }
        divider = (uint32_t)strtoul(argv[3], NULL, 0);
        width   = (uint32_t)strtoul(argv[4], NULL, 0);
        delay   = (uint32_t)strtoul(argv[5], NULL, 0);
        if (argc == 9) {
            phase_freq = (uint32_t)strtoul(argv[6], NULL, 0);
            phase_amp_q15 = (uint32_t)strtoul(argv[7], NULL, 0);
            control    = (uint32_t)strtoul(argv[8], NULL, 0);
        } else if (argc == 8) {
            phase_freq = (uint32_t)strtoul(argv[6], NULL, 0);
            phase_amp_q15 = rd32(base, REG_PHASE_AMP);
            control    = (uint32_t)strtoul(argv[7], NULL, 0);
        } else {
            phase_freq = rd32(base, REG_PHASE_FREQ);
            phase_amp_q15 = rd32(base, REG_PHASE_AMP);
            /* Keep bits 0 (enable) and 2 (phase_mod_enable) from the argument */
            control    = ((uint32_t)strtoul(argv[6], NULL, 0) & 0x7u);
        }

        /* Disable output before changing parameters to avoid glitches */
        wr32(base, REG_CONTROL, 0);
        wr32(base, REG_DIVIDER, divider);
        wr32(base, REG_WIDTH, width);
        wr32(base, REG_DELAY, delay);
        wr32(base, REG_PHASE_FREQ, phase_freq);
        wr32(base, REG_PHASE_AMP, phase_amp_q15);
        /* Mask: bit0=pulse_enable, bit2=phase_mod_enable; bit1 (soft_reset) self-clears */
        wr32(base, REG_CONTROL, control & 0x7u);

        print_json(base);

    } else if (strcmp(argv[2], "soft_reset") == 0) {
        const uint32_t control = rd32(base, REG_CONTROL) & ~0x2u;
        /* Bit 1 is self-clearing in the FPGA (single-cycle strobe) — one write is enough.
         * Preserve bits 0 and 2 (enable / phase_mod_enable). */
        wr32(base, REG_CONTROL, control | 0x2u);
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
