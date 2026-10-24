// Device-side (GPU-resident) form of the validated density-prefilter engine
// (density_prefilter.h + terrain_shaper.h). Same pattern as device_noise.h: build on
// the host (needs MD5/std::string/std::vector/shared_ptr), convert to fixed-size POD
// structs, upload once, sample on device for every region checked.
#pragma once
#include "device_noise.h"
#include "terrain_shaper.h"
#include "density_prefilter.h"

#ifdef __CUDACC__
#define DD_HD __host__ __device__
#else
#define DD_HD
#endif

// Slack in the density prefilter accept test; tunable so it can be measured, not assumed.
#ifndef DENSITY_MARGIN
#define DENSITY_MARGIN 8
#endif

// [bytecode-verified] PerlinNoiseSampler.sample(x,y,z,yScale,yMax) -- the full 5-arg
// form with the yScale-driven fy2 branch, needed by InterpolatedNoiseSampler (device_noise.h's
// dev_perlinSample only implements the yScale==0 shortcut used by check()).
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
    // [bytecode-verified, matches the already-fixed host perlin_noise.h::sample/
    // sampleAndLerp] the Y FADE term uses the ORIGINAL, unmodified fy -- NOT fy2, and
    // NOT (fy-fy2). Only the gradient dot products use the fy2-adjusted value. Getting
    // this wrong (using fy2, which is 0 whenever yScale==0) silently flattens all Y
    // interpolation -- exactly the bug this project already found and fixed once in the
    // host path; reintroduced here when this device function was written fresh.
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

// [bytecode-verified] OctavePerlinNoiseSampler.getOctave(i) = octaveSamplers[count-1-i]
// (the reversed accessor InterpolatedNoiseSampler actually uses).
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

// [bytecode-verified] InterpolatedNoiseSampler.calculateNoise -- identical formula to
// the validated host path (density_prefilter.h), operating on Dev* fixed-size types.
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

// All 14 check()-path samplers are reused via DevNoiseCache (already uploaded);
// this struct carries only what the density prefilter adds on top.
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
    // c.terrainParams is now the SAME shared (seed-independent) tree for every seed (see
    // DensityContext::build in density_prefilter.h), so flattening it is a pure function
    // of a constant input -- do the ~74-node tree walk once, not once per seed. Still a
    // cheap POD struct-copy into d.terrainParams every call, just not the walk itself.
    static const DevSplineTable cachedTable = flattenTerrainParameters(c.terrainParams);
    d.terrainParams = cachedTable;
    return d;
}

DD_HD inline double dev_sampleShiftNoise(const DevDensityContext& dc, double x, double y, double z) {
    return dev_sample(dc.shiftNoise, x, y, z) * 4.0;
}

struct DevNoiseInfo { double shiftedX, shiftedZ, offset, factor, peaks; };

// [bytecode-verified] createNoiseInfo -- NOTE shiftedZ's (cellZ, cellX, 0) argument
// order, and createTerrainNoisePoint(continentalness, weirdness, erosion) ->
// createNoisePoint(continentalness, erosion, weirdness).
DD_HD inline DevNoiseInfo dev_createNoiseInfo(const DevNoiseCache& nc, const DevDensityContext& dc, int cellX, int cellZ) {
    DevNoiseInfo info;
    info.shiftedX = cellX + dev_sampleShiftNoise(dc, cellX, 0, cellZ);
    info.shiftedZ = cellZ + dev_sampleShiftNoise(dc, cellZ, cellX, 0);
    double continentalness = dev_sample(nc.continentalness, info.shiftedX, 0, info.shiftedZ);
    double weirdness = dev_sample(nc.ridge, info.shiftedX, 0, info.shiftedZ);
    double erosion = dev_sample(nc.erosion, info.shiftedX, 0, info.shiftedZ);
    NoisePointF np = VanillaTerrainParameters::createNoisePoint((float)continentalness, (float)erosion, (float)weirdness);
    // NOTE: -0.50375f must be a float literal -- dev_splineApply returns float, and
    // subtracting a bare (double) literal here would silently upgrade the arithmetic to
    // double precision, more accurate than Java's actual float-only computation.
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

// outMean, when non-null, receives the 5-probe ladder mean this gate cut on -- the GPU's own
// estimate of the hut floor. Used to measure its error against Phase 2's exact value.
DD_HD inline bool dev_passesDensityPrefilter(const DevNoiseCache& nc, const DevDensityContext& dc,
                                             int64_t seed, int hutX, int hutZ, int maxHeight,
                                             float* outMean = nullptr) {
    bool rotated = isHutRotated90(seed, hutX, hutZ);
    int sum = 0, np = 0;
#ifdef DENSITY_PROBES_FULL
    // MEASURED 2026-08-22 AND REJECTED -- do not enable. See the numbers below the loop.
    // FULL FOOTPRINT. Phase 2 averages the column top over all 63 hut-footprint columns
    // (7x9), but this gate estimated the same quantity from only 5 probes -- so it needed a
    // wide +8 margin just to cover its own sampling error, and that margin is what lets ~500
    // candidates per seed through to a CPU stage costing ~16ms each.
    //
    // Sampling all 63 makes the GPU estimate the same statistic Phase 2 computes, which
    // should permit a much tighter cut. The GPU can afford it: this runs only on dev_check
    // survivors (~1900/seed), so 63 probes costs ~120k ladder evaluations per seed against
    // the 1.373e10 erosion evaluations the scan already does -- utterly negligible there,
    // while every candidate it removes saves real CPU time on the binding stage.
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
    // DENSITY_PROBES_FULL result: at the inherited +8 margin it drops candidates to 57% but
    // LOSES REAL HUTS (2 of 34, including a -54 from the 3x-54 record seed) -- a better
    // estimator does not inherit the old estimator.s margin. Re-derived: the full-footprint
    // mean needs margin 20 before losses stop, and at 20 it returns 97% of baseline
    // candidates. The extra probes shift the estimate.s BIAS more than they cut its variance,
    // so the wider margin it requires eats the entire gain. 5 probes at +8 is near-optimal.
    double mean = sum / (double)np;
    if (outMean) *outMean = (float)mean;
    // Slack between this 5-probe ladder estimate and the real hut floor. NOTE: density_prefilter.h
    // carries a HOST copy of this same test -- both must move together or --self-test diverges.
    return !(mean > maxHeight + DENSITY_MARGIN);
}
