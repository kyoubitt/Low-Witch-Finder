#pragma once
#include "device_noise.h"
#include "terrain_shaper.h"
#include "density_prefilter.h"

#ifdef __CUDACC__
#define DD_HD __host__ __device__
#else
#define DD_HD
#endif

#ifndef DENSITY_MARGIN
#define DENSITY_MARGIN 8
#endif

DD_HD inline double dev_perlinSample5(const DevPerlin& p, double x, double y, double z, double yScale, double yMax) {
    double x2 = x + p.originX, y2 = y + p.originY, z2 = z + p.originZ;
    int ix = (int)floor(x2), iy = (int)floor(y2), iz = (int)floor(z2);
    double fx = x2 - ix, fy = y2 - iy, fz = z2 - iz;
    double fy2;
    if (yScale != 0.0) {
        double clampedY = (yMax >= 0.0 && yMax < fy) ? yMax : fy;
        fy2 = floor(clampedY / yScale + 1.0E-7) * yScale;
    } else {
        fy2 = 0.0;
    }

    double useFy = fy - fy2;
    int a  = dev_getGradient(p, ix);
    int a1 = dev_getGradient(p, ix + 1);
    int b  = dev_getGradient(p, a + iy);
    int b1 = dev_getGradient(p, a + iy + 1);
    int c  = dev_getGradient(p, a1 + iy);
    int c1 = dev_getGradient(p, a1 + iy + 1);
    double v000 = pn_grad(dev_getGradient(p, b + iz), fx, useFy, fz);
    double v100 = pn_grad(dev_getGradient(p, c + iz), fx - 1.0, useFy, fz);
    double v010 = pn_grad(dev_getGradient(p, b1 + iz), fx, useFy - 1.0, fz);
    double v110 = pn_grad(dev_getGradient(p, c1 + iz), fx - 1.0, useFy - 1.0, fz);
    double v001 = pn_grad(dev_getGradient(p, b + iz + 1), fx, useFy, fz - 1.0);
    double v101 = pn_grad(dev_getGradient(p, c + iz + 1), fx - 1.0, useFy, fz - 1.0);
    double v011 = pn_grad(dev_getGradient(p, b1 + iz + 1), fx, useFy - 1.0, fz - 1.0);
    double v111 = pn_grad(dev_getGradient(p, c1 + iz + 1), fx - 1.0, useFy - 1.0, fz - 1.0);
    return pn_lerp3(pn_fade(fx), pn_fade(fy), pn_fade(fz),
                    v000, v100, v010, v110, v001, v101, v011, v111);
}

#define DEV_LEGACY_MAX_OCTAVES 16

struct DevLegacyOctave {
    int octaveCount;
    double lacunarity, persistence;
    bool hasOctave[DEV_LEGACY_MAX_OCTAVES];
    DevPerlin samplers[DEV_LEGACY_MAX_OCTAVES];
};

inline DevLegacyOctave toDev(const LegacyOctave& o) {
    DevLegacyOctave d;
    d.octaveCount = (int)o.amplitudes.size();
    d.lacunarity = o.lacunarity;
    d.persistence = o.persistence;
    for (int i = 0; i < d.octaveCount; i++) {
        d.hasOctave[i] = o.hasOctave[i];
        if (o.hasOctave[i]) d.samplers[i] = toDev(o.octaveSamplers[i]);
    }
    return d;
}

DD_HD inline const DevPerlin* dev_getOctave(const DevLegacyOctave& o, int i) {
    int idx = o.octaveCount - 1 - i;
    return o.hasOctave[idx] ? &o.samplers[idx] : nullptr;
}

struct DevInterpolatedNoiseSampler {
    DevLegacyOctave lowerInterpolatedNoise, upperInterpolatedNoise, interpolationNoise;
    double xzScale, yScale, xzMainScale, yMainScale;
    int cellWidth, cellHeight;
};

inline DevInterpolatedNoiseSampler toDev(const InterpolatedNoiseSampler& s) {
    DevInterpolatedNoiseSampler d;
    d.lowerInterpolatedNoise = toDev(s.lowerInterpolatedNoise);
    d.upperInterpolatedNoise = toDev(s.upperInterpolatedNoise);
    d.interpolationNoise = toDev(s.interpolationNoise);
    d.xzScale = s.xzScale; d.yScale = s.yScale;
    d.xzMainScale = s.xzMainScale; d.yMainScale = s.yMainScale;
    d.cellWidth = s.cellWidth; d.cellHeight = s.cellHeight;
    return d;
}

DD_HD inline int dev_floorDiv(int a, int b) {
    int q = a / b;
    if ((a % b != 0) && ((a < 0) != (b < 0))) q--;
    return q;
}

DD_HD inline double dev_calculateTerrainNoise(const DevInterpolatedNoiseSampler& s, int x, int y, int z) {
    int cellX = dev_floorDiv(x, s.cellWidth);
    int cellY = dev_floorDiv(y, s.cellHeight);
    int cellZ = dev_floorDiv(z, s.cellWidth);

    double d = 0.0, amp = 1.0;
    for (int i = 0; i < 8; i++) {
        const DevPerlin* oct = dev_getOctave(s.interpolationNoise, i);
        if (oct) {
            double fx = pn_maintainPrecision(cellX * s.xzMainScale * amp);
            double fy = pn_maintainPrecision(cellY * s.yMainScale * amp);
            double fz = pn_maintainPrecision(cellZ * s.xzMainScale * amp);
            double yScaleArg = s.yMainScale * amp;
            double yMaxArg = cellY * s.yMainScale * amp;
            d += dev_perlinSample5(*oct, fx, fy, fz, yScaleArg, yMaxArg) / amp;
        }
        amp /= 2.0;
    }
    double t = ((d / 10.0) + 1.0) / 2.0;
    bool useUpperOnly = t >= 1.0;
    bool useLowerOnly = t <= 0.0;

    double lowerSum = 0.0, upperSum = 0.0;
    amp = 1.0;
    for (int i = 0; i < 16; i++) {
        double fx = pn_maintainPrecision(cellX * s.xzScale * amp);
        double fy = pn_maintainPrecision(cellY * s.yScale * amp);
        double fz = pn_maintainPrecision(cellZ * s.xzScale * amp);
        double yScaleArg2 = s.yScale * amp;
        double yMaxArg2 = cellY * yScaleArg2;
        if (!useUpperOnly) {
            const DevPerlin* oct = dev_getOctave(s.lowerInterpolatedNoise, i);
            if (oct) lowerSum += dev_perlinSample5(*oct, fx, fy, fz, yScaleArg2, yMaxArg2) / amp;
        }
        if (!useLowerOnly) {
            const DevPerlin* oct = dev_getOctave(s.upperInterpolatedNoise, i);
            if (oct) upperSum += dev_perlinSample5(*oct, fx, fy, fz, yScaleArg2, yMaxArg2) / amp;
        }
        amp /= 2.0;
    }
    return mh_clampedLerp(lowerSum / 512.0, upperSum / 512.0, t) / 128.0;
}

struct DevDensityContext {
    DevDoublePerlin shiftNoise;
    DevDoublePerlin jaggedNoise;
    DevInterpolatedNoiseSampler terrainNoise;
    DevSplineTable terrainParams;
};

inline DevDensityContext toDev(const DensityContext& c) {
    DevDensityContext d;
    d.shiftNoise = toDev(c.shiftNoise);
    d.jaggedNoise = toDev(c.jaggedNoise);
    d.terrainNoise = toDev(c.terrainNoise);

    static const DevSplineTable cachedTable = flattenTerrainParameters(c.terrainParams);
    d.terrainParams = cachedTable;
    return d;
}

DD_HD inline double dev_sampleShiftNoise(const DevDensityContext& dc, double x, double y, double z) {
    return dev_sample(dc.shiftNoise, x, y, z) * 4.0;
}

struct DevNoiseInfo { double shiftedX, shiftedZ, offset, factor, peaks; };

DD_HD inline DevNoiseInfo dev_createNoiseInfo(const DevNoiseCache& nc, const DevDensityContext& dc, int cellX, int cellZ) {
    DevNoiseInfo info;
    info.shiftedX = cellX + dev_sampleShiftNoise(dc, cellX, 0, cellZ);
    info.shiftedZ = cellZ + dev_sampleShiftNoise(dc, cellZ, cellX, 0);
    double continentalness = dev_sample(nc.continentalness, info.shiftedX, 0, info.shiftedZ);
    double weirdness = dev_sample(nc.ridge, info.shiftedX, 0, info.shiftedZ);
    double erosion = dev_sample(nc.erosion, info.shiftedX, 0, info.shiftedZ);
    NoisePointF np = VanillaTerrainParameters::createNoisePoint((float)continentalness, (float)erosion, (float)weirdness);

    info.offset = dev_splineApply(dc.terrainParams, dc.terrainParams.offsetRoot, np) - 0.50375f;
    info.factor = dev_splineApply(dc.terrainParams, dc.terrainParams.factorRoot, np);
    info.peaks = dev_splineApply(dc.terrainParams, dc.terrainParams.peakRoot, np);
    return info;
}

DD_HD inline double dev_sampleJaggedNoise(const DevDensityContext& dc, double peaks, double shiftedX, double shiftedZ) {
    if (peaks == 0.0) return 0.0;
    double j = dev_sample(dc.jaggedNoise, shiftedX * 1500.0, 0, shiftedZ * 1500.0);
    if (j > 0.0) return peaks * j;
    return (peaks / 2.0) * j;
}

DD_HD inline double dev_clampedMap(double value, double oldMin, double oldMax, double newMin, double newMax) {
    return mh_clampedLerp(newMin, newMax, (value - oldMin) / (oldMax - oldMin));
}
DD_HD inline double dev_noiseGradientDensity(double factor, double depth) {
    double f = depth * factor;
    return f > 0.0 ? f * 4.0 : f;
}

DD_HD inline double dev_sampleSlopedCheese(const DevNoiseCache& nc, const DevDensityContext& dc, int x, int y, int z) {
    int cellX = x >> 2, cellZ = z >> 2;
    DevNoiseInfo info = dev_createNoiseInfo(nc, dc, cellX, cellZ);
    double jagged = dev_sampleJaggedNoise(dc, info.peaks, info.shiftedX, info.shiftedZ);
    double depth = info.offset + dev_clampedMap(y, -64, 320, 1.5, -1.5);
    double density = dev_noiseGradientDensity(info.factor, depth + jagged);
    density += dev_calculateTerrainNoise(dc.terrainNoise, cellX, y, cellZ);
    return density;
}

DD_HD inline double dev_cheeseWithSloped(const DevNoiseCache& nc, const DevDensityContext& dc, int x, int y, int z) {
    double layer = 4.0 * dev_sample(nc.caveLayer, x, y * 8.0, z) * dev_sample(nc.caveLayer, x, y * 8.0, z);
    double caveCheeseVal = mh_clamp(0.27 + dev_sample(nc.caveCheese, x, y * 0.6666666666666666, z), -1.0, 1.0);
    double sloped = dev_sampleSlopedCheese(nc, dc, x, y, z);
    double slopedTerm = mh_clamp(1.5 + (-0.64 * sloped), 0.0, 0.5);
    return layer + caveCheeseVal + slopedTerm;
}

DD_HD inline bool dev_isDensityLadderAir(const DevNoiseCache& nc, const DevDensityContext& dc, int x, int y, int z) {
    double cheese = dev_cheeseWithSloped(nc, dc, x, y, z);
    if (y > 50) return dev_Entrance(nc, x, y, z) < 0.0;
    if (y >= 10) return dev_Entrance(nc, x, y, z) < 0.0 || cheese < 0.0;
    return dev_Entrance2(nc, x, y, z) < 0.0 || cheese < 0.0;
}

DD_HD inline int dev_estimateCheeseSurfaceLadder(const DevNoiseCache& nc, const DevDensityContext& dc, int x, int z) {
    if (!dev_isDensityLadderAir(nc, dc, x, 60, z)) {
        if (!dev_isDensityLadderAir(nc, dc, x, 70, z)) return 70;
        return 63;
    }
    if (!dev_isDensityLadderAir(nc, dc, x, 50, z)) return 60;
    const int LADDER[10] = {40, 30, 20, 10, 0, -10, -20, -30, -40, -50};
    for (int i = 0; i < 10; i++) {
        if (!dev_isDensityLadderAir(nc, dc, x, LADDER[i], z)) {
            if (i == 0) return 50;
            return LADDER[i - 1];
        }
    }
    return -54;
}

DD_HD inline bool dev_passesDensityPrefilter(const DevNoiseCache& nc, const DevDensityContext& dc,
                                             int64_t seed, int hutX, int hutZ, int maxHeight,
                                             float* outMean = nullptr) {
    bool rotated = isHutRotated90(seed, hutX, hutZ);
    int sum = 0, np = 0;
#ifdef DENSITY_PROBES_FULL

    for (int a = 0; a < 7; a++) {
        for (int b = 0; b < 9; b++) {
            int wx, wz;
            if (!rotated) { wx = hutX + a; wz = hutZ + b; }
            else          { wx = hutX + b; wz = hutZ + a; }
            sum += dev_estimateCheeseSurfaceLadder(nc, dc, wx, wz);
            np++;
        }
    }
#else
    const int PROBES[5][2] = {{3, 4}, {3, 1}, {3, 7}, {1, 4}, {5, 4}};
    for (int i = 0; i < 5; i++) {
        int wx, wz;
        if (!rotated) { wx = hutX + PROBES[i][0]; wz = hutZ + PROBES[i][1]; }
        else { wx = hutX + PROBES[i][1]; wz = hutZ + PROBES[i][0]; }
        sum += dev_estimateCheeseSurfaceLadder(nc, dc, wx, wz);
        np++;
    }
#endif

    double mean = sum / (double)np;
    if (outMean) *outMean = (float)mean;

    return !(mean > maxHeight + DENSITY_MARGIN);
}
