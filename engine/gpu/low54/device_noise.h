// Device-side (GPU-resident) form of the validated noise engine.
//
// The host headers (perlin_noise.h / noise_setup.h) use std::vector, which cannot live in
// device code. This header carries the SAME bit-exact math over to fixed-size POD structs
// that can be memcpy'd to the GPU and indexed inside a kernel.
//
// Construction still happens ON THE HOST (it needs MD5 + string names), then the finished
// tables are uploaded. Only sampling runs on device -- which is the expensive part, and
// the part that's identical for every one of the billions of coordinates scanned.
#pragma once
#include "noise_setup.h"
#include <cstdint>

#ifdef __CUDACC__
#define DN_HD __host__ __device__
#else
#define DN_HD
#endif

// Largest amplitude count across all keys used (check()'s continentalness/cave_cheese
// need 9; the density prefilter's JAGGED noise needs 16).
#define DN_MAX_OCTAVES 16

struct DevPerlin {
    double originX, originY, originZ;
    // y==0 constants. Climate samples all enter with y=0, so y2 == originY and its floor
    // and fraction are fixed per sampler -- recomputing them per region was a floor and a
    // subtract of pure waste on the hot path. See dev_perlinSampleY0.
    int    iy0;
    double fy0;
    uint8_t permutations[256];
};

struct DevOctave {
    int    octaveCount;                  // == amplitudes length
    double amplitudes[DN_MAX_OCTAVES];
    // Per-octave frequency/amplitude, tabulated. The sampling loop used to walk these with
    // freq *= 2.0 and amp /= 2.0 every octave. Both are exact power-of-two scalings of
    // lacunarity/persistence, so tabulating them by the SAME walk changes no bits.
    double freqAt[DN_MAX_OCTAVES];
    double ampAt[DN_MAX_OCTAVES];
    bool   hasOctave[DN_MAX_OCTAVES];
    DevPerlin samplers[DN_MAX_OCTAVES];  // only entries with hasOctave[i] are valid
    double lacunarity;
    double persistence;
};

struct DevDoublePerlin {
    DevOctave first;
    DevOctave second;
    double amplitude;
};

// All 14 samplers check() needs, in one uploadable blob.
struct DevNoiseCache {
    DevDoublePerlin caveEntrance, spaghettiRarity, spaghettiThickness;
    DevDoublePerlin spaghetti3D1, spaghetti3D2;
    DevDoublePerlin spaghettiRoughnessModulator, spaghettiRoughness;
    DevDoublePerlin erosion, temperature, continentalness, ridge;
    DevDoublePerlin caveLayer, caveCheese, aquiferFloodedness;
};

// The 3 samplers dev_check() tests FIRST (erosion/temperature/ridge -- see dev_check
// below), split out on their own so they can be built for every seed cheaply while the
// other 11 (~74% of a seed's total sampler cost) are built only for seeds that have at
// least one region passing all three of these. See constructClimateKernel/
// climateCheckKernel in phase1_scan.cu.
struct DevClimateCache {
    DevDoublePerlin erosion, temperature, ridge;
};

// ---------- host-side conversion from the validated std::vector form ----------
DN_HD inline DevPerlin toDev(const PerlinNoiseSampler& p) {
    DevPerlin d;
    d.originX = p.originX; d.originY = p.originY; d.originZ = p.originZ;
    d.iy0 = (int)floor(p.originY);          // exactly what dev_perlinSampleY0 computed
    d.fy0 = p.originY - d.iy0;              // per call, for y == 0
    for (int i = 0; i < 256; i++) d.permutations[i] = p.permutations[i];
    return d;
}

inline DevOctave toDev(const OctavePerlinNoiseSampler& o) {
    DevOctave d;
    d.octaveCount = (int)o.amplitudes.size();
    if (d.octaveCount > DN_MAX_OCTAVES) {
        std::fprintf(stderr, "octave count %d exceeds DN_MAX_OCTAVES\n", d.octaveCount);
        std::abort();
    }
    double lf = o.lacunarity, la = o.persistence;
    for (int i = 0; i < d.octaveCount; i++) {
        d.amplitudes[i] = o.amplitudes[i];
        d.hasOctave[i]  = o.hasOctave[i];
        d.freqAt[i] = lf; d.ampAt[i] = la;   // same walk the sampling loop performed
        lf *= 2.0; la /= 2.0;
        if (o.hasOctave[i]) d.samplers[i] = toDev(o.octaveSamplers[i]);
    }
    d.lacunarity = o.lacunarity;
    d.persistence = o.persistence;
    return d;
}

inline DevDoublePerlin toDev(const DoublePerlinNoiseSampler& s) {
    DevDoublePerlin d;
    d.first = toDev(s.firstSampler);
    d.second = toDev(s.secondSampler);
    d.amplitude = s.amplitude;
    return d;
}

inline DevNoiseCache toDev(const WorldNoiseCache& c) {
    DevNoiseCache d;
    d.caveEntrance = toDev(c.caveEntrance);
    d.spaghettiRarity = toDev(c.spaghettiRarity);
    d.spaghettiThickness = toDev(c.spaghettiThickness);
    d.spaghetti3D1 = toDev(c.spaghetti3D1);
    d.spaghetti3D2 = toDev(c.spaghetti3D2);
    d.spaghettiRoughnessModulator = toDev(c.spaghettiRoughnessModulator);
    d.spaghettiRoughness = toDev(c.spaghettiRoughness);
    d.erosion = toDev(c.erosion);
    d.temperature = toDev(c.temperature);
    d.continentalness = toDev(c.continentalness);
    d.ridge = toDev(c.ridge);
    d.caveLayer = toDev(c.caveLayer);
    d.caveCheese = toDev(c.caveCheese);
    d.aquiferFloodedness = toDev(c.aquiferFloodedness);
    return d;
}

// ---------- device-callable sampling (identical math to the validated host path) ----------
DN_HD inline int dev_getGradient(const DevPerlin& p, int x) { return p.permutations[x & 255] & 255; }

// PHASE1_FP32_CORE: run the INTERPOLATION CORE of a Perlin sample in single precision.
//
// This card does FP64 at 1/67th of FP32 (measured: 1.47 vs 98.71 TFLOP/s), and the scan is
// FP64-throughput bound -- which is why it sits at 100% "utilisation" while drawing 177W of
// a 600W budget: the FP64 units are a thin slice of the die, and saturating them pegs the
// counter while the rest of the chip idles.
//
// What may NOT move to float: the coordinate reduction. maintainPrecision folds coordinates
// into +-2^25 (3.3554432e7), and at that magnitude a float's ULP is 2.0 -- x2 - floor(x2)
// would round to a constant and every fractional position in the lattice cell would
// collapse. So x2/y2/z2, their floors, and the subtraction that produces the fractions stay
// in double, exactly as before.
//
// What may: everything downstream. fx/fy/fz land in [0,1), the gradient dot products in
// [-2,2], and the fades and lerps never leave that range -- float carries ~7 significant
// digits there, against a noise threshold comparison that cares about ~2 decimal places.
// That covers the 8 grad calls, 3 fades and 7 lerps, roughly 70 of the ~100 FP ops per
// octave, leaving only the ~30 coordinate ops in double.
//
// The error this introduces is real but bounded and one-directional in effect: it can only
// shift a sample by ~1e-6, which matters solely for coordinates sitting within that of a
// check() threshold. Phase 1 is a superset filter re-verified exactly by Phase 2, so the
// only failure mode that costs anything is a FALSE NEGATIVE, and that is what the
// superset validation against the FP64 build is there to rule out.
#ifdef PHASE1_FP32_CORE
typedef float  dn_core_t;
#define DN_C(x) ((float)(x))
#else
typedef double dn_core_t;
#define DN_C(x) (x)
#endif

DN_HD inline dn_core_t dnc_lerp(dn_core_t t, dn_core_t a, dn_core_t b) { return a + t * (b - a); }
DN_HD inline dn_core_t dnc_fade(dn_core_t t) {
    return t * t * t * (t * (t * DN_C(6.0) - DN_C(15.0)) + DN_C(10.0));
}
DN_HD inline dn_core_t dnc_grad(int hash, dn_core_t x, dn_core_t y, dn_core_t z) {
    int h = hash & 15;
    dn_core_t u = h < 8 ? x : y;
    dn_core_t v = h < 4 ? y : (h == 12 || h == 14 ? x : z);
    return ((h & 1) == 0 ? u : -u) + ((h & 2) == 0 ? v : -v);
}

DN_HD inline double dev_perlinSample(const DevPerlin& p, double x, double y, double z) {
    // --- stays in double: magnitudes up to 2^25 live here ---
    double x2 = x + p.originX, y2 = y + p.originY, z2 = z + p.originZ;
    int ix = (int)floor(x2), iy = (int)floor(y2), iz = (int)floor(z2);
    // yScale == 0 for every caller in check(), so fy2 == 0 and the fade uses fy directly.
    dn_core_t fx = (dn_core_t)(x2 - ix), fy = (dn_core_t)(y2 - iy), fz = (dn_core_t)(z2 - iz);

    // --- from here on everything is in [-2,2] and runs in dn_core_t ---
    const dn_core_t ONE = DN_C(1.0);
    int a  = dev_getGradient(p, ix);
    int a1 = dev_getGradient(p, ix + 1);
    int b  = dev_getGradient(p, a + iy);
    int b1 = dev_getGradient(p, a + iy + 1);
    int c  = dev_getGradient(p, a1 + iy);
    int c1 = dev_getGradient(p, a1 + iy + 1);
    dn_core_t v000 = dnc_grad(dev_getGradient(p, b + iz), fx, fy, fz);
    dn_core_t v100 = dnc_grad(dev_getGradient(p, c + iz), fx - ONE, fy, fz);
    dn_core_t v010 = dnc_grad(dev_getGradient(p, b1 + iz), fx, fy - ONE, fz);
    dn_core_t v110 = dnc_grad(dev_getGradient(p, c1 + iz), fx - ONE, fy - ONE, fz);
    dn_core_t v001 = dnc_grad(dev_getGradient(p, b + iz + 1), fx, fy, fz - ONE);
    dn_core_t v101 = dnc_grad(dev_getGradient(p, c + iz + 1), fx - ONE, fy, fz - ONE);
    dn_core_t v011 = dnc_grad(dev_getGradient(p, b1 + iz + 1), fx, fy - ONE, fz - ONE);
    dn_core_t v111 = dnc_grad(dev_getGradient(p, c1 + iz + 1), fx - ONE, fy - ONE, fz - ONE);
    dn_core_t tx = dnc_fade(fx), ty = dnc_fade(fy), tz = dnc_fade(fz);
    return (double)dnc_lerp(tz, dnc_lerp(ty, dnc_lerp(tx, v000, v100), dnc_lerp(tx, v010, v110)),
                                dnc_lerp(ty, dnc_lerp(tx, v001, v101), dnc_lerp(tx, v011, v111)));
}

// ---------- y == 0 specialisation ----------
// Every climate sample -- erosion, temperature, ridge, continentalness -- is taken at y=0
// (see dev_check). That makes the entire Y coordinate chain a constant that the general
// path recomputes for all 1.373e10 regions:
//     0 * freq                      == 0
//     maintainPrecision(0)          == 0 - floor(0/2^25 + 0.5)*2^25 == 0   (floor(0.5)==0)
//     y2 = 0 + originY              == originY
// so y2, iy and fy depend only on the sampler. This drops the y multiply, the whole
// maintainPrecision call and the y2 add -- 7 of the ~27 remaining DOUBLE ops per octave,
// and double is what this kernel is bound by.
//
// Bit-identical by construction, not by approximation: every step above is an exact
// identity, so this must produce the same candidates as the general path. It is worth
// specialising because the timing is flat in max-height (13.2s at -52 vs 12.8s at 0 while
// candidates go 340 -> 16493), which shows the per-region climate gates ARE the runtime
// and everything downstream is noise.
// ---------- DN_BITSPLIT: floor and fraction of a double from its bit pattern ----------
//
// `ix = (int)floor(x2); fx = (dn_core_t)(x2 - ix)` is four double-pipe ops per coordinate per
// octave (round-to-integer, convert, convert back, subtract, then narrow). The double add that
// PRODUCES x2 is left exactly as it was, so x2's rounding is unchanged; only the split of an
// already-rounded x2 moves to integer ops. That makes this an identity on x2, not an
// approximation, and it is proved as one: see the --noise-identity harness.
//
// With x2 = (-1)^s * M * 2^(e-52), M the 53-bit significand and sh = 52 - e:
//   integer part  I  = M >> sh
//   fraction bits fr = M & (2^sh - 1)          g = fr / 2^sh, exact
//   x2 >= 0:  ix = I,            x2 - ix = g                      (exact, a subtraction of an
//                                                                  integer from a larger-ulp value)
//   x2 <  0:  ix = -I - 1 (or -I when fr == 0), x2 - ix = 1 - g = (2^sh - fr) / 2^sh
// The negative case is exact in double ONLY while 1 - g is representable, i.e. the ulp of x2
// is at least 2^-53, i.e. e >= -1. Below that the original's subtraction rounds and this
// would not, so e < -1 takes the original code. The upper bound is just keeping I inside int.
// Both exits are taken for ~1e-6 of coordinates, so the divergence they cause is nil.
//
// dn_core_t(fr) is a round-to-nearest-even conversion of an integer < 2^53, scaled by an exact
// power of two: the same single rounding the original's narrowing performs on the same real.
#ifdef __CUDA_ARCH__
__device__ __forceinline__ unsigned long long dn_bits(double d) { return (unsigned long long)__double_as_longlong(d); }
__device__ __forceinline__ double dn_from_bits(unsigned long long u) { return __longlong_as_double((long long)u); }

__device__ __forceinline__ void dn_split(double x2, int& ix, dn_core_t& fx) {
    const unsigned long long u = dn_bits(x2);
    const int e = (int)((u >> 52) & 0x7FFULL) - 1023;
#ifdef DN_MUTATE_BOUND
    if ((unsigned)(e + 4) > 34u) {
#else
    if ((unsigned)(e + 1) > 31u) {                       // e outside [-1, 30]
#endif
        ix = (int)floor(x2);
        fx = (dn_core_t)(x2 - ix);
        return;
    }
    const unsigned long long M = (u & 0xFFFFFFFFFFFFFULL) | (1ULL << 52);
    const int sh = 52 - e;                               // 22 .. 53
    const unsigned long long fr = M & ((1ULL << sh) - 1ULL);
    const int I = (int)(M >> sh);
#ifdef PHASE1_FP32_CORE
    const float scale = __int_as_float((127 - sh) << 23);                    // 2^-sh
#else
    const double scale = __longlong_as_double((long long)(1023 - sh) << 52);
#endif
    if ((long long)u >= 0) {
        ix = I;
        fx = (dn_core_t)fr * scale;
    } else if (fr == 0ULL) {
        ix = -I;
        fx = (dn_core_t)0;
    } else {
        ix = -I - 1;
        fx = (dn_core_t)((1ULL << sh) - fr) * scale;
    }
}
#endif

// REFERENCE: the version before DN_BITSPLIT, kept verbatim. The identity harness compares
// against it, and it is what the host build runs.
DN_HD inline double dev_perlinSampleY0_ref(const DevPerlin& p, double x, double z) {
    double x2 = x + p.originX, z2 = z + p.originZ;
    int ix = (int)floor(x2), iz = (int)floor(z2);
    const int iy = p.iy0;                        // floor(originY), fixed per sampler
    dn_core_t fx = (dn_core_t)(x2 - ix), fy = (dn_core_t)p.fy0, fz = (dn_core_t)(z2 - iz);

    const dn_core_t ONE = DN_C(1.0);
    int a  = dev_getGradient(p, ix);
    int a1 = dev_getGradient(p, ix + 1);
    int b  = dev_getGradient(p, a + iy);
    int b1 = dev_getGradient(p, a + iy + 1);
    int c  = dev_getGradient(p, a1 + iy);
    int c1 = dev_getGradient(p, a1 + iy + 1);
    dn_core_t v000 = dnc_grad(dev_getGradient(p, b + iz), fx, fy, fz);
    dn_core_t v100 = dnc_grad(dev_getGradient(p, c + iz), fx - ONE, fy, fz);
    dn_core_t v010 = dnc_grad(dev_getGradient(p, b1 + iz), fx, fy - ONE, fz);
    dn_core_t v110 = dnc_grad(dev_getGradient(p, c1 + iz), fx - ONE, fy - ONE, fz);
    dn_core_t v001 = dnc_grad(dev_getGradient(p, b + iz + 1), fx, fy, fz - ONE);
    dn_core_t v101 = dnc_grad(dev_getGradient(p, c + iz + 1), fx - ONE, fy, fz - ONE);
    dn_core_t v011 = dnc_grad(dev_getGradient(p, b1 + iz + 1), fx, fy - ONE, fz - ONE);
    dn_core_t v111 = dnc_grad(dev_getGradient(p, c1 + iz + 1), fx - ONE, fy - ONE, fz - ONE);
    dn_core_t tx = dnc_fade(fx), ty = dnc_fade(fy), tz = dnc_fade(fz);
    return (double)dnc_lerp(tz, dnc_lerp(ty, dnc_lerp(tx, v000, v100), dnc_lerp(tx, v010, v110)),
                                dnc_lerp(ty, dnc_lerp(tx, v001, v101), dnc_lerp(tx, v011, v111)));
}

DN_HD inline double dev_perlinSampleY0(const DevPerlin& p, double x, double z) {
    double x2 = x + p.originX, z2 = z + p.originZ;
    int ix, iz;
    dn_core_t fx, fz;
#ifdef __CUDA_ARCH__
    dn_split(x2, ix, fx);
    dn_split(z2, iz, fz);
#else
    ix = (int)floor(x2); iz = (int)floor(z2);
    fx = (dn_core_t)(x2 - ix); fz = (dn_core_t)(z2 - iz);
#endif
    const int iy = p.iy0;                        // floor(originY), fixed per sampler
    dn_core_t fy = (dn_core_t)p.fy0;

    const dn_core_t ONE = DN_C(1.0);
    int a  = dev_getGradient(p, ix);
    int a1 = dev_getGradient(p, ix + 1);
    int b  = dev_getGradient(p, a + iy);
    int b1 = dev_getGradient(p, a + iy + 1);
    int c  = dev_getGradient(p, a1 + iy);
    int c1 = dev_getGradient(p, a1 + iy + 1);
    dn_core_t v000 = dnc_grad(dev_getGradient(p, b + iz), fx, fy, fz);
    dn_core_t v100 = dnc_grad(dev_getGradient(p, c + iz), fx - ONE, fy, fz);
    dn_core_t v010 = dnc_grad(dev_getGradient(p, b1 + iz), fx, fy - ONE, fz);
    dn_core_t v110 = dnc_grad(dev_getGradient(p, c1 + iz), fx - ONE, fy - ONE, fz);
    dn_core_t v001 = dnc_grad(dev_getGradient(p, b + iz + 1), fx, fy, fz - ONE);
    dn_core_t v101 = dnc_grad(dev_getGradient(p, c + iz + 1), fx - ONE, fy, fz - ONE);
    dn_core_t v011 = dnc_grad(dev_getGradient(p, b1 + iz + 1), fx, fy - ONE, fz - ONE);
    dn_core_t v111 = dnc_grad(dev_getGradient(p, c1 + iz + 1), fx - ONE, fy - ONE, fz - ONE);
    dn_core_t tx = dnc_fade(fx), ty = dnc_fade(fy), tz = dnc_fade(fz);
    return (double)dnc_lerp(tz, dnc_lerp(ty, dnc_lerp(tx, v000, v100), dnc_lerp(tx, v010, v110)),
                                dnc_lerp(ty, dnc_lerp(tx, v001, v101), dnc_lerp(tx, v011, v111)));
}

// maintainPrecision folds a coordinate into +-2^24 and is a NO-OP whenever |v| is already
// under that. For the climate samplers -- the ones on this y=0 path, and the ones every
// region pays for -- it can never fire, provably:
//
//   input is climateX/4.0 with |climateX| <= 3.0e7 (world border), so |x| <= 7.5e6
//   the second sampler scales by f = 1.0181268882175227
//   max frequency is lacunarity * 2^(octaves-1):
//        erosion         2^-5  ->  |x*freq| <= 2.39e5
//        temperature     2^-5  ->              2.39e5
//        ridge           2^-2  ->              1.91e6
//        continentalness 2^-1  ->              3.82e6
//   all against a 2^24 = 1.678e7 threshold -- the worst case has 4.4x of headroom.
//
// So the call is pure overhead here: a divide, an add, a floor, a multiply and a subtract,
// twice per octave, which is roughly half of ALL remaining FP64 work in the hot loop.
// DEV_Y0_ASSUME_NO_WRAP drops it. dev_octaveSample (the general 3D path, used by the cave
// and aquifer samplers at large y and unbounded coordinates) keeps maintainPrecision --
// those genuinely can wrap.
//
// The bound depends on the world border. phase1_scan.cu asserts the requested region range
// stays inside it at startup, so widening the search past +-30M fails loudly instead of
// silently corrupting the noise.
#define DN_Y0_WRAP_LIMIT 1.6777216e7

// REFERENCE octave loop (pre-DN_EXPSHIFT), kept verbatim for the identity harness and the host.
DN_HD inline double dev_octaveSampleY0_ref(const DevOctave& o, double x, double z) {
    double result = 0.0;
    for (int i = 0; i < o.octaveCount; i++) {
        if (o.hasOctave[i]) {
            const double freq = o.freqAt[i];
#ifdef DEV_Y0_ASSUME_NO_WRAP
            double sx = x * freq;
            double sz = z * freq;
#else
            double sx = pn_maintainPrecision(x * freq);
            double sz = pn_maintainPrecision(z * freq);
#endif
            // Expression ORDER preserved: amplitude * sample * amp. Folding amplitudes[i]
            // and ampAt[i] into one weight would reassociate the product and could flip a bit.
            result += o.amplitudes[i] * dev_perlinSampleY0_ref(o.samplers[i], sx, sz) * o.ampAt[i];
        }
    }
    return result;
}

// DN_EXPSHIFT: `x * freq` as an integer add on the exponent field.
//
// freqAt[i] is lacunarity * 2^i, lacunarity = 2^firstOctave, so every freq is an exact power of
// two, and multiplying a normal double by 2^k is exactly adding k to its biased exponent. The
// bit pattern of freq minus the bit pattern of 1.0 IS that k, shifted into place -- one 64-bit
// subtract, no double-pipe op. Valid while x is normal and non-zero and the result stays
// normal: x = 4*cx+2 is a non-zero integer, x*f for the second sampler is non-zero, and the
// smallest scaling is 2^-10, so |x*freq| >= 2^-9. A zero x (never produced by any caller)
// takes the reference loop rather than being trusted.
#ifdef __CUDA_ARCH__
DN_HD inline double dev_octaveSampleY0(const DevOctave& o, double x, double z) {
#ifndef DEV_Y0_ASSUME_NO_WRAP
    return dev_octaveSampleY0_ref(o, x, z);
#else
    const unsigned long long xb = dn_bits(x), zb = dn_bits(z);
    if ((xb << 1) == 0ULL || (zb << 1) == 0ULL) return dev_octaveSampleY0_ref(o, x, z);
    double result = 0.0;
    for (int i = 0; i < o.octaveCount; i++) {
        if (o.hasOctave[i]) {
            const unsigned long long d = dn_bits(o.freqAt[i]) - 0x3FF0000000000000ULL;
            const double sx = dn_from_bits(xb + d);
            const double sz = dn_from_bits(zb + d);
            result += o.amplitudes[i] * dev_perlinSampleY0(o.samplers[i], sx, sz) * o.ampAt[i];
        }
    }
    return result;
#endif
}
#else
DN_HD inline double dev_octaveSampleY0(const DevOctave& o, double x, double z) {
    return dev_octaveSampleY0_ref(o, x, z);
}
#endif

// ---------- STAGED EROSION: the exact early exit ----------
//
// Erosion rejects 96.1% of starts, and 90% of its cost is the 8 octave samples every start pays
// before learning that. A warp-level early exit inside one kernel is worthless (a warp waits for
// its slowest lane, and nothing correlates 32 random starts), so the exit is taken BETWEEN two
// kernels instead: kernel A evaluates only the heaviest octaves, rejects, and compacts the
// survivors; kernel B finishes only those. Compaction is what makes a data-dependent exit pay.
//
// EXACTNESS. The reference gate sums octaves in index order into two double accumulators and
// tests `(a + b) * amplitude < 0.55`. Stage A runs the SAME loop over octave indices [0, S) from
// 0.0 and hands its two accumulators over; stage B continues the SAME loop from index S with
// those accumulators as the starting values. The operation sequence, and so every rounding, is
// the one the unstaged loop performs -- B's result is bit-identical, not merely close.
//
// The EXIT is the only inexact-looking step, and it is a one-sided rigorous bound: a start is
// rejected after stage A only if even the largest total the unrun octaves could add still leaves
// the gate below its threshold. With M a bound on |sample| and tail = sum over unrun octaves of
// |amplitude| * ampAt * M (both samplers), the final value satisfies
//        V <= (aPartial + bPartial + tail) * amplitude          (+ ~1e-15 of double rounding)
// so (aPartial + bPartial + tail) * amplitude < 0.55 - 1e-9 implies V < 0.55 with the 1e-9
// swallowing every rounding. It can only ever reject starts the full gate would also reject.
//
// M IS NOT 1. The improved-Perlin value is a weighted average of corner dot products, and a
// corner's dot product with one of these gradients reaches 2, so the sup is above 1:
//   dense grid, N=512 over (fx,fy,fz):   sup |noise| = 1.0364  (at fx=.356 fy=.482 fz=.500)
//   Lipschitz bound |grad| <= sqrt(3)*(2*1.875*2 + 1) = 14.7 over a half-diagonal of 0.00169
//        -> grid error <= 0.025  ->  rigorous sup <= 1.0613
//   float-core rounding adds ~1e-6.   M = 1.07 clears both.
// An exit built on |sample| <= 1 would have rejected real survivors.
#ifndef DN_PERLIN_ABS_MAX                      // overridable ONLY so the mutation test can break it
#define DN_PERLIN_ABS_MAX 1.07
#endif
#define DN_EROSION_SPLIT 2       // stage A covers octave indices [0, 2)
#define DN_EROSION_THRESHOLD 0.55 // the gate's own constant

// The unrun-octave bound for ONE octave set.
DN_HD inline double dev_octaveTail(const DevOctave& o, int from) {
    double t = 0.0;
    for (int i = from; i < o.octaveCount; i++)
        if (o.hasOctave[i]) t += fabs(o.amplitudes[i]) * o.ampAt[i] * DN_PERLIN_ABS_MAX;
    return t;
}
DN_HD inline double dev_erosionTail(const DevDoublePerlin& s) {
    return dev_octaveTail(s.first, DN_EROSION_SPLIT) + dev_octaveTail(s.second, DN_EROSION_SPLIT);
}

// The octave loop over [from, to) continuing from `acc`. Same body, same expression order, same
// exponent-add scaling as dev_octaveSampleY0, so a [0,count) call IS that function.
DN_HD inline double dev_octaveRangeY0(const DevOctave& o, double x, double z,
                                      int from, int to, double acc) {
#ifdef __CUDA_ARCH__
    const unsigned long long xb = dn_bits(x), zb = dn_bits(z);
    for (int i = from; i < to; i++) {
        if (o.hasOctave[i]) {
            const unsigned long long d = dn_bits(o.freqAt[i]) - 0x3FF0000000000000ULL;
            const double sx = dn_from_bits(xb + d);
            const double sz = dn_from_bits(zb + d);
            acc += o.amplitudes[i] * dev_perlinSampleY0(o.samplers[i], sx, sz) * o.ampAt[i];
        }
    }
#else
    for (int i = from; i < to; i++) {
        if (o.hasOctave[i]) {
            const double freq = o.freqAt[i];
            acc += o.amplitudes[i] * dev_perlinSampleY0(o.samplers[i], x * freq, z * freq) * o.ampAt[i];
        }
    }
#endif
    return acc;
}

DN_HD inline void dev_erosionStageA(const DevNoiseCache& c, int x, int z, double& ap, double& bp) {
    const double f = 1.0181268882175227;
    const double X = (x + 8) / 4.0, Z = (z + 8) / 4.0;
    ap = dev_octaveRangeY0(c.erosion.first,  X,     Z,     0, DN_EROSION_SPLIT, 0.0);
    bp = dev_octaveRangeY0(c.erosion.second, X * f, Z * f, 0, DN_EROSION_SPLIT, 0.0);
}
DN_HD inline bool dev_erosionStageAReject(const DevNoiseCache& c, double ap, double bp, double tail) {
    return (ap + bp + tail) * c.erosion.amplitude < DN_EROSION_THRESHOLD - 1e-9;
}
DN_HD inline bool dev_erosionStageB(const DevNoiseCache& c, int x, int z, double ap, double bp) {
    const double f = 1.0181268882175227;
    const double X = (x + 8) / 4.0, Z = (z + 8) / 4.0;
    double a = dev_octaveRangeY0(c.erosion.first,  X,     Z,     DN_EROSION_SPLIT, c.erosion.first.octaveCount,  ap);
    double b = dev_octaveRangeY0(c.erosion.second, X * f, Z * f, DN_EROSION_SPLIT, c.erosion.second.octaveCount, bp);
    return !((a + b) * c.erosion.amplitude < DN_EROSION_THRESHOLD);
}

DN_HD inline double dev_sampleY0_ref(const DevDoublePerlin& s, double x, double z) {
    const double f = 1.0181268882175227;
    double a = dev_octaveSampleY0_ref(s.first, x, z);
    double b = dev_octaveSampleY0_ref(s.second, x * f, z * f);
    return (a + b) * s.amplitude;
}

DN_HD inline double dev_sampleY0(const DevDoublePerlin& s, double x, double z) {
    const double f = 1.0181268882175227;
    double a = dev_octaveSampleY0(s.first, x, z);
    double b = dev_octaveSampleY0(s.second, x * f, z * f);   // y * f == 0 too
    return (a + b) * s.amplitude;
}
DN_HD inline double dev_octaveSample(const DevOctave& o, double x, double y, double z) {
    double result = 0.0;
    double freq = o.lacunarity;
    double amp = o.persistence;
    for (int i = 0; i < o.octaveCount; i++) {
        if (o.hasOctave[i]) {
            double sx = pn_maintainPrecision(x * freq);
            double sy = pn_maintainPrecision(y * freq);
            double sz = pn_maintainPrecision(z * freq);
            result += o.amplitudes[i] * dev_perlinSample(o.samplers[i], sx, sy, sz) * amp;
        }
        freq *= 2.0;
        amp /= 2.0;
    }
    return result;
}

DN_HD inline double dev_sample(const DevDoublePerlin& s, double x, double y, double z) {
    const double f = 1.0181268882175227;
    double a = dev_octaveSample(s.first, x, y, z);
    double b = dev_octaveSample(s.second, x * f, y * f, z * f);
    return (a + b) * s.amplitude;
}

// ---------- device-side Entrance / Entrance2 / Cheese ----------
DN_HD inline double dev_lerpFromProgress(const DevDoublePerlin& s, double x, double y, double z,
                                          double lo, double hi) {
    double v = dev_sample(s, x, y, z);
    return pn_lerp((v + 1.0) / 2.0, lo, hi);
}

DN_HD inline double dev_Entrance(const DevNoiseCache& c, int x, int y, int z) {
    double cc = dev_sample(c.caveEntrance, x * 0.75, y * 0.5, z * 0.75) + 0.37
              + mh_clampedLerp(0.3, 0.0, (10 + (double)y) / 40.0);
    double d = dev_sample(c.spaghettiRarity, x * 2.0, y, z * 2.0);
    double e = scaleTunnels(d);
    double h = dev_lerpFromProgress(c.spaghettiThickness, x, y, z, 0.065, 0.088);
    double l = dev_sample(c.spaghetti3D1, x / e, y / e, z / e);
    double m = fabs(e * l) - h;
    double n = dev_sample(c.spaghetti3D2, x / e, y / e, z / e);
    double o = fabs(e * n) - h;
    double p = mh_clamp(m > o ? m : o, -1.0, 1.0);
    double q = (-0.05 + (-0.05 * dev_sample(c.spaghettiRoughnessModulator, x, y, z)))
             * (-0.4 + fabs(dev_sample(c.spaghettiRoughness, x, y, z)));
    double pq = p + q;
    return cc < pq ? cc : pq;
}

DN_HD inline double dev_Entrance2(const DevNoiseCache& c, int x, int y, int z) {
    double d = dev_sample(c.spaghettiRarity, x * 2.0, y, z * 2.0);
    double e = scaleTunnels(d);
    double h = dev_lerpFromProgress(c.spaghettiThickness, x, y, z, 0.065, 0.088);
    double l = dev_sample(c.spaghetti3D1, x / e, y / e, z / e);
    double m = fabs(e * l) - h;
    double n = dev_sample(c.spaghetti3D2, x / e, y / e, z / e);
    double o = fabs(e * n) - h;
    double p = mh_clamp(m > o ? m : o, -1.0, 1.0);
    double q = (-0.05 + (-0.05 * dev_sample(c.spaghettiRoughnessModulator, x, y, z)))
             * (-0.4 + fabs(dev_sample(c.spaghettiRoughness, x, y, z)));
    return p + q;
}

DN_HD inline double dev_Cheese(const DevNoiseCache& c, int x, int y, int z) {
    double layer = dev_sample(c.caveLayer, x, y * 8.0, z);
    double a = 4.0 * layer * layer;
    double b = mh_clamp(0.27 + dev_sample(c.caveCheese, x, y * 0.6666666666666666, z), -1.0, 1.0);
    return a + b;
}

// ---------- SearchCoords.check() ----------
// Returns true if the coordinate survives Phase 1 (i.e. becomes a Phase 2 candidate).
// gameVersionIs26_2 toggles the one extra ridge rejection that GameVersion.V26_2 adds.
// SPLIT INTO TWO HALVES, and dev_check below is now literally their conjunction, so the
// equivalence is structural rather than something a test has to establish. The gates are
// ANDed and every sampler is a pure function of (cache, coordinate), so partitioning them
// changes only the order of evaluation -- which was already documented as a free cost choice.
//
// WHY: roomCheckKernel costs 114 registers, which on sm_89 is 2 blocks of 256 per SM, i.e.
// 512 of 1536 threads -- 33% occupancy. About 99% of starts die on the first climate gate,
// yet every one of those threads is charged the full register footprint of the column tests
// it never reaches. Running the climate half as its own low-register kernel lets the common
// path run at full occupancy, and the expensive half then sees only the survivors. This is
// the same fused-vs-split reasoning that put the sweep and the check in separate kernels; it
// applies twice because the cost is concentrated in a stage that is reached rarely.
//
// The climate half takes the CLIMATE samplers (erosion, temperature, ridge,
// continentalness); the column half takes everything that samples a y column (Entrance,
// Cheese, aquifer floodedness). Continentalness moves from last to the climate group, which
// is a reorder of ANDed predicates and nothing more.
// ONE PREDICATE PER GATE, so the order below is the ONLY thing that encodes the order, and a
// profiler can measure each gate's rejection rate without a second copy of the logic that
// could drift from this one. Cost is 2 x (active octaves) Perlin evaluations per sampler:
//
//     erosion          {1,1,0,1,1}            4 active ->  8 evals
//     temperature      {1.5,0,1,0,0,0}        2 active ->  4 evals
//     ridge            {1,2,1,0,0,0}          3 active ->  6 evals
//     continentalness  {1,1,2,2,2,1,1,1,1}    9 active -> 18 evals   <- most expensive by far
//
// For ANDed gates the expected-cost-minimising order sorts by cost/(1-pass), so the cheap
// hard-rejecting gates belong first and continentalness, at 18 evals, belongs last unless it
// rejects far harder than the rest. `--gate-profile` measures exactly that.
DN_HD inline bool dev_gateErosion(const DevNoiseCache& c, int x, int z) {
    return !(dev_sampleY0(c.erosion, (x + 8) / 4.0, (z + 8) / 4.0) < 0.55);
}
DN_HD inline bool dev_gateTemperature(const DevNoiseCache& c, int x, int z) {
    double t = dev_sampleY0(c.temperature, (x + 8) / 4.0, (z + 8) / 4.0);
    return !(t > 0.2 || t < -0.45);                               // non-1.18.2 branch
}
DN_HD inline bool dev_gateRidge(const DevNoiseCache& c, int x, int z,
                                bool gameVersionIs26_2) {
    double r = dev_sampleY0(c.ridge, (x + 8) / 4.0, (z + 8) / 4.0);
    if ((r > 0.42 && r < 0.91) || (r < -0.42 && r > -0.91)) return false;
    if (gameVersionIs26_2 && r <= -0.91) return false;
    return true;
}
DN_HD inline bool dev_gateContinentalness(const DevNoiseCache& c, int x, int z) {
    return !(dev_sampleY0(c.continentalness, (x + 8) / 4.0, (z + 8) / 4.0) < -0.11);
}

DN_HD inline bool dev_checkClimate(const DevNoiseCache& c, int x, int z,
                                   bool gameVersionIs26_2) {
#ifdef GATE_TEMP_FIRST
    // MEASURED SLOWER, twice: 12.65 vs 12.02 s and 12.40 vs 11.42 s on the fused kernel, and
    // 7.56 vs 7.32 s after the climate/column split. Erosion rejects hard enough to be worth
    // its 8 evals up front. Kept only so the claim stays re-testable.
    if (!dev_gateTemperature(c, x, z)) return false;
    if (!dev_gateErosion(c, x, z)) return false;
#else
    if (!dev_gateErosion(c, x, z)) return false;
    if (!dev_gateTemperature(c, x, z)) return false;
#endif
    if (!dev_gateRidge(c, x, z, gameVersionIs26_2)) return false;
    if (!dev_gateContinentalness(c, x, z)) return false;
    return true;
}

DN_HD inline bool dev_checkColumn(const DevNoiseCache& c, int x, int z, int maxHeight) {
    int heightX = x + 3, heightZ = z + 3;

    if (dev_Entrance(c, heightX, 50, heightZ) >= 0) return false;
    if (dev_Entrance(c, heightX, 60, heightZ) >= 0) return false;

    if (dev_Entrance2(c, heightX, maxHeight, heightZ) >= 0
        && dev_Cheese(c, heightX, maxHeight, heightZ) >= 0) return false;

    // Lower bound must track maxHeight, not a hardcoded -40 -- see SearchCoords.check()'s
    // Java copy of this same loop for the full rationale. Behaviorally identical to the old
    // -40 bound for any maxHeight >= -40 (the inner `maxHeight < y` guard already no-ops the
    // extra iterations); only adds real coverage for maxHeight deeper than -40.
    for (int y = 0; y >= maxHeight; y -= 10) {
        if (maxHeight < y) {
            if (dev_Entrance2(c, heightX, y, heightZ) >= 0
                && dev_Cheese(c, heightX, y, heightZ) >= 0) return false;
        }
    }
    for (int y = 10; y <= 40; y += 10) {
        if (dev_Entrance(c, heightX, y, heightZ) >= 0
            && dev_Cheese(c, heightX, y, heightZ) >= 0) return false;
    }

    for (int y = maxHeight; y <= 60; y += 10) {
        if (dev_sample(c.aquiferFloodedness, heightX, y * 0.67, heightZ) > 0.41) return false;
    }
    return true;
}

DN_HD inline bool dev_check(const DevNoiseCache& c, int x, int z, int maxHeight,
                            bool gameVersionIs26_2) {
    return dev_checkClimate(c, x, z, gameVersionIs26_2)
        && dev_checkColumn(c, x, z, maxHeight);
}

// The original single-body form, kept verbatim and unused, as the reference the split above is
// a refactor OF. If the two ever need re-reconciling, diff against this.
DN_HD inline bool dev_checkMonolithic(const DevNoiseCache& c, int x, int z, int maxHeight,
                                      bool gameVersionIs26_2) {
    int climateX = x + 8, climateZ = z + 8;
    int heightX = x + 3, heightZ = z + 3;

    // Gate order is a pure cost choice -- these are ANDed, so any order yields the same
    // candidates. Cost per sampler is (active octaves x 2) Perlin evaluations:
    // temperature {1.5,0,1,0,0,0} = 4, ridge {1,2,1,0,0,0} = 6, erosion {1,1,0,1,1} = 8.
    // GATE_TEMP_FIRST tests the cheapest gate first. MEASURED 2026-08-21 and it is SLOWER:
    // 12.65s vs 12.02s and 12.40s vs 11.42s on full-world seeds (identical candidates).
    // Erosion rejects hard enough that paying its 8 evals up front beats paying 4 then 8
    // for the majority of regions. Erosion-first is optimal -- do not swap it again.
#ifdef GATE_TEMP_FIRST
    double temperature = dev_sampleY0(c.temperature, climateX / 4.0, climateZ / 4.0);
    if (temperature > 0.2 || temperature < -0.45) return false;   // non-1.18.2 branch

    double erosionSample = dev_sampleY0(c.erosion, climateX / 4.0, climateZ / 4.0);
    if (erosionSample < 0.55) return false;
#else
    double erosionSample = dev_sampleY0(c.erosion, climateX / 4.0, climateZ / 4.0);
    if (erosionSample < 0.55) return false;

    double temperature = dev_sampleY0(c.temperature, climateX / 4.0, climateZ / 4.0);
    if (temperature > 0.2 || temperature < -0.45) return false;   // non-1.18.2 branch
#endif

    double ridge = dev_sampleY0(c.ridge, climateX / 4.0, climateZ / 4.0);
    if ((ridge > 0.42 && ridge < 0.91) || (ridge < -0.42 && ridge > -0.91)) return false;
    if (gameVersionIs26_2 && ridge <= -0.91) return false;

    if (dev_Entrance(c, heightX, 50, heightZ) >= 0) return false;
    if (dev_Entrance(c, heightX, 60, heightZ) >= 0) return false;

    if (dev_Entrance2(c, heightX, maxHeight, heightZ) >= 0
        && dev_Cheese(c, heightX, maxHeight, heightZ) >= 0) return false;

    // Lower bound must track maxHeight, not a hardcoded -40 -- see SearchCoords.check()'s
    // Java copy of this same loop for the full rationale. Behaviorally identical to the old
    // -40 bound for any maxHeight >= -40 (the inner `maxHeight < y` guard already no-ops the
    // extra iterations); only adds real coverage for maxHeight deeper than -40.
    for (int y = 0; y >= maxHeight; y -= 10) {
        if (maxHeight < y) {
            if (dev_Entrance2(c, heightX, y, heightZ) >= 0
                && dev_Cheese(c, heightX, y, heightZ) >= 0) return false;
        }
    }
    for (int y = 10; y <= 40; y += 10) {
        if (dev_Entrance(c, heightX, y, heightZ) >= 0
            && dev_Cheese(c, heightX, y, heightZ) >= 0) return false;
    }

    if (dev_sampleY0(c.continentalness, climateX / 4.0, climateZ / 4.0) < -0.11) return false;

    for (int y = maxHeight; y <= 60; y += 10) {
        if (dev_sample(c.aquiferFloodedness, heightX, y * 0.67, heightZ) > 0.41) return false;
    }
    return true;
}

