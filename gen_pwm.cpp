/*
 * gen_pwm.cpp — Generate a fixed PWM signal on OUT1
 *
 * Uses the board's built-in generate binary which correctly initialises
 * the DAC hardware. Duty cycle is approximated via rise/fall time ratio.
 *
 * Build:  g++ -std=c++20 -I/boot/include -o gen_pwm gen_pwm.cpp \
 *             -L/boot/lib -Wl,-rpath,/boot/lib -lrp -lrp-hw-calib
 * Run:    /opt/redpitaya/sbin/overlay.sh v0.94 && ./gen_pwm
 */

#include <cstdio>
#include <cstdlib>
#include <unistd.h>
#include <rp.h>
#include <rp_hw_calib.h>

#define FREQ_HZ   8000.0f
#define DUTY      0.1f
#define AMP       1.0f      /* Vpeak */

int main()
{
    /* Step 1: use generate to fully initialise the DAC hardware */
    system("/opt/redpitaya/bin/generate 1 1.0 8000 sqr");
    usleep(100000);  /* 100 ms settle */

    /* Step 2: take over with rp API (no reset — preserve DAC state) */
    if (rp_InitReset(false) != RP_OK) {
        fprintf(stderr, "rp_InitReset failed\n");
        return 1;
    }
    rp_CalibInit();

    rp_GenWaveform(RP_CH_1, RP_WAVEFORM_PWM);
    rp_GenFreq(RP_CH_1, FREQ_HZ);
    rp_GenAmp(RP_CH_1, AMP);
    rp_GenOffset(RP_CH_1, 0.0f);
    rp_GenPhase(RP_CH_1, 0.0f);
    rp_GenOutEnable(RP_CH_1);
    rp_GenDutyCycle(RP_CH_1, DUTY);

    printf("OUT1: %.0f Hz PWM, %.0f%% duty, %.1f Vpeak — running (Ctrl+C to stop)\n",
           (double)FREQ_HZ, (double)(DUTY * 100), (double)AMP);
    fflush(stdout);

    for (;;) sleep(60);

    rp_GenOutDisable(RP_CH_1);
    rp_Release();
    return 0;
}
