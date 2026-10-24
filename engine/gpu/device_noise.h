#pragma once
#include "noise_setup.h"
#include <cstdint>

#ifdef __CUDACC__
#define DN_HD __host__ __device__
#else
#define DN_HD
#endif

#define DN_MAX_OCTAVES 16

struct DevPerlin {
    double originX, originY, originZ;

    int    iy0;
    double fy0;
    uint8_t permutations[256];
};

struct DevOctave {
    int    octaveCount;
    double amplitudes[DN_MAX_OCTAVES];

    double freqAt[DN_MAX_OCTAVES];
    double ampAt[DN_MAX_OCTAVES];
    bool   hasOctave[DN_MAX_OCTAVES];
    DevPerlin samplers[DN_MAX_OCTAVES];
    double lacunarity;
    double persistence;
};

struct DevDoublePerlin {
    DevOctave first;
    DevOctave second;
    double amplitude;
};

struct DevNoiseCache {
    DevDoublePerlin caveEntrance, spaghettiRarity, spaghettiThickness;
    DevDoublePerlin spaghetti3D1, spaghetti3D2;
    DevDoublePerlin spaghettiRoughnessModulator, spaghettiRoughness;
    DevDoublePerlin erosion, temperature, continentalness, ridge;
    DevDoublePerlin caveLayer, caveCheese, aquiferFloodedness;
};

struct DevClimateCache {
    DevDoublePerlin erosion, temperature, ridge;
};

DN_HD inline void dev_fillPerlin(DevPerlin& d, const PerlinNoiseSampler& p) {
    d.originX = p.originX; d.originY = p.originY; d.originZ = p.originZ;
    d.iy0 = (int)floor(p.originY);
    d.fy0 = p.originY - d.iy0;
#ifdef __CUDA_ARCH__
    const unsigned long long* s = reinterpret_cast<const unsigned long long*>(p.permutations);
    unsigned long long* t = reinterpret_cast<unsigned long long*>(d.permutations);
#pragma unroll
    for (int i = 0; i < 32; i++) t[i] = s[i];
#else
    for (int i = 0; i < 256; i++) d.permutations[i] = p.permutations[i];
#endif
}

DN_HD inline DevPerlin toDev(const PerlinNoiseSampler& p) {
    DevPerlin d;
    d.originX = p.originX; d.originY = p.originY; d.originZ = p.originZ;
    d.iy0 = (int)floor(p.originY);
    d.fy0 = p.originY - d.iy0;
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
        d.freqAt[i] = lf; d.ampAt[i] = la;
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

DN_HD inline int dev_getGradient(const DevPerlin& p, int x) { return p.permutations[x & 255] & 255; }

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

    double x2 = x + p.originX, y2 = y + p.originY, z2 = z + p.originZ;
    int ix = (int)floor(x2), iy = (int)floor(y2), iz = (int)floor(z2);

    dn_core_t fx = (dn_core_t)(x2 - ix), fy = (dn_core_t)(y2 - iy), fz = (dn_core_t)(z2 - iz);

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

#ifdef __CUDA_ARCH__
__device__ __forceinline__ unsigned long long dn_bits(double d) { return (unsigned long long)__double_as_longlong(d); }
__device__ __forceinline__ double dn_from_bits(unsigned long long u) { return __longlong_as_double((long long)u); }

__device__ __forceinline__ void dn_split(double x2, int& ix, dn_core_t& fx) {
    const unsigned long long u = dn_bits(x2);
    const int e = (int)((u >> 52) & 0x7FFULL) - 1023;
#ifdef DN_MUTATE_BOUND
    if ((unsigned)(e + 4) > 34u) {
#else
    if ((unsigned)(e + 1) > 31u) {
#endif
        ix = (int)floor(x2);
        fx = (dn_core_t)(x2 - ix);
        return;
    }
    const unsigned long long M = (u & 0xFFFFFFFFFFFFFULL) | (1ULL << 52);
    const int sh = 52 - e;
    const unsigned long long fr = M & ((1ULL << sh) - 1ULL);
    const int I = (int)(M >> sh);
#ifdef PHASE1_FP32_CORE
    const float scale = __int_as_float((127 - sh) << 23);
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

DN_HD inline double dev_perlinSampleY0_ref(const DevPerlin& p, double x, double z) {
    double x2 = x + p.originX, z2 = z + p.originZ;
    int ix = (int)floor(x2), iz = (int)floor(z2);
    const int iy = p.iy0;
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
    const int iy = p.iy0;
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

#define DN_Y0_WRAP_LIMIT 1.6777216e7

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

            result += o.amplitudes[i] * dev_perlinSampleY0_ref(o.samplers[i], sx, sz) * o.ampAt[i];
        }
    }
    return result;
}

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

#ifndef DN_PERLIN_ABS_MAX
#define DN_PERLIN_ABS_MAX 1.07
#endif
#define DN_EROSION_SPLIT 2
#define DN_EROSION_THRESHOLD 0.55

DN_HD inline double dev_octaveTail(const DevOctave& o, int from) {
    double t = 0.0;
    for (int i = from; i < o.octaveCount; i++)
        if (o.hasOctave[i]) t += fabs(o.amplitudes[i]) * o.ampAt[i] * DN_PERLIN_ABS_MAX;
    return t;
}
DN_HD inline double dev_erosionTail(const DevDoublePerlin& s) {
    return dev_octaveTail(s.first, DN_EROSION_SPLIT) + dev_octaveTail(s.second, DN_EROSION_SPLIT);
}

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
    double b = dev_octaveSampleY0(s.second, x * f, z * f);
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

DN_HD inline bool dev_gateErosion(const DevNoiseCache& c, int x, int z) {
    return !(dev_sampleY0(c.erosion, (x + 8) / 4.0, (z + 8) / 4.0) < 0.55);
}
DN_HD inline bool dev_gateTemperature(const DevNoiseCache& c, int x, int z) {
    double t = dev_sampleY0(c.temperature, (x + 8) / 4.0, (z + 8) / 4.0);
    return !(t > 0.2 || t < -0.45);
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

DN_HD inline bool dev_checkMonolithic(const DevNoiseCache& c, int x, int z, int maxHeight,
                                      bool gameVersionIs26_2) {
    int climateX = x + 8, climateZ = z + 8;
    int heightX = x + 3, heightZ = z + 3;

#ifdef GATE_TEMP_FIRST
    double temperature = dev_sampleY0(c.temperature, climateX / 4.0, climateZ / 4.0);
    if (temperature > 0.2 || temperature < -0.45) return false;

    double erosionSample = dev_sampleY0(c.erosion, climateX / 4.0, climateZ / 4.0);
    if (erosionSample < 0.55) return false;
#else
    double erosionSample = dev_sampleY0(c.erosion, climateX / 4.0, climateZ / 4.0);
    if (erosionSample < 0.55) return false;

    double temperature = dev_sampleY0(c.temperature, climateX / 4.0, climateZ / 4.0);
    if (temperature > 0.2 || temperature < -0.45) return false;
#endif

    double ridge = dev_sampleY0(c.ridge, climateX / 4.0, climateZ / 4.0);
    if ((ridge > 0.42 && ridge < 0.91) || (ridge < -0.42 && ridge > -0.91)) return false;
    if (gameVersionIs26_2 && ridge <= -0.91) return false;

    if (dev_Entrance(c, heightX, 50, heightZ) >= 0) return false;
    if (dev_Entrance(c, heightX, 60, heightZ) >= 0) return false;

    if (dev_Entrance2(c, heightX, maxHeight, heightZ) >= 0
        && dev_Cheese(c, heightX, maxHeight, heightZ) >= 0) return false;

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
