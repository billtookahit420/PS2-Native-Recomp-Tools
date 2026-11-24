/* ps2run was built against glibc 2.43, which re-versioned five float math
   functions. Defining them here (via the double-precision originals, whose
   symbol versions are ancient) drops the binary's floor to glibc 2.39. */
#include <math.h>
float asinf(float x) { return (float)asin((double)x); }
float acosf(float x) { return (float)acos((double)x); }
float atan2f(float y, float x) { return (float)atan2((double)y, (double)x); }
float sinhf(float x) { return (float)sinh((double)x); }
float coshf(float x) { return (float)cosh((double)x); }
