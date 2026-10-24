// Port of VanillaTerrainParameters / Spline (Minecraft's spline-based "TerrainShaper"),
// used only by sampleSlopedCheese (the density prefilter). Every formula and constant
// below was pulled directly from javap -c bytecode of VanillaTerrainParameters.class /
// Spline.class (com.github.KalleStruik:noise-sampler:1.20.0), not recalled from memory --
// there are too many individual float constants here to trust recall alone.
#pragma once
#include <cmath>
#include <memory>
#include <vector>
#include <cstdio>
#include <cstdlib>

#ifdef __CUDACC__
#define TS_HD __host__ __device__
#else
#define TS_HD
#endif

struct NoisePointF {
    float continentalness, erosion, normalizedWeirdness, weirdness;
};

enum class SplineAxis { CONTINENTS, EROSION, WEIRDNESS, RIDGES };

TS_HD inline float ts_coord(SplineAxis a, const NoisePointF& p) {
    switch (a) {
        case SplineAxis::CONTINENTS: return p.continentalness;
        case SplineAxis::EROSION: return p.erosion;
        case SplineAxis::WEIRDNESS: return p.weirdness;
        case SplineAxis::RIDGES: return p.normalizedWeirdness;
    }
    return 0.0f;
}

TS_HD inline float ts_lerp(float t, float a, float b) { return a + t * (b - a); }

// [bytecode-verified] Spline$SplineImpl.apply -- cubic Hermite-style interpolation
// between control points, linear extrapolation with the endpoint derivative beyond
// the first/last point.
struct Spline {
    bool isConst = false;
    float constValue = 0.0f;
    SplineAxis axis = SplineAxis::CONTINENTS;
    std::vector<float> locations;
    std::vector<std::shared_ptr<Spline>> values;
    std::vector<float> derivatives;

    static std::shared_ptr<Spline> constant(float v) {
        auto s = std::make_shared<Spline>();
        s->isConst = true;
        s->constValue = v;
        return s;
    }

    float apply(const NoisePointF& p) const {
        if (isConst) return constValue;
        float x = ts_coord(axis, p);
        int last = (int)locations.size() - 1;
        // last index i such that locations[i] <= x (locations are always ascending)
        int idx = -1;
        for (int i = 0; i <= last; i++) {
            if (locations[i] <= x) idx = i; else break;
        }
        if (idx < 0) {
            return values[0]->apply(p) + derivatives[0] * (x - locations[0]);
        }
        if (idx == last) {
            return values[last]->apply(p) + derivatives[last] * (x - locations[last]);
        }
        float x0 = locations[idx], x1 = locations[idx + 1];
        float t = (x - x0) / (x1 - x0);
        float y0 = values[idx]->apply(p);
        float y1 = values[idx + 1]->apply(p);
        float d0 = derivatives[idx], d1 = derivatives[idx + 1];
        float k0 = d0 * (x1 - x0) - (y1 - y0);
        float k1 = -d1 * (x1 - x0) + (y1 - y0);
        return ts_lerp(t, y0, y1) + t * (1.0f - t) * ts_lerp(t, k0, k1);
    }
};
using SplinePtr = std::shared_ptr<Spline>;

struct SplineBuilder {
    SplineAxis axis;
    std::vector<float> locations;
    std::vector<SplinePtr> values;
    std::vector<float> derivatives;
    explicit SplineBuilder(SplineAxis a) : axis(a) {}
    SplineBuilder& add(float loc, float val, float deriv) {
        locations.push_back(loc); values.push_back(Spline::constant(val)); derivatives.push_back(deriv);
        return *this;
    }
    SplineBuilder& add(float loc, const SplinePtr& child, float deriv) {
        locations.push_back(loc); values.push_back(child); derivatives.push_back(deriv);
        return *this;
    }
    SplinePtr build() {
        auto s = std::make_shared<Spline>();
        s->isConst = false; s->axis = axis;
        s->locations = locations; s->values = values; s->derivatives = derivatives;
        return s;
    }
};

// [bytecode-verified] VanillaTerrainParameters.getNormalizedWeirdness
TS_HD inline float getNormalizedWeirdness(float f) {
    return -(std::fabs(std::fabs(f) - 0.6666667f) - 0.33333334f) * 3.0f;
}

// [bytecode-verified] method_38857(f): tiny 2-point WEIRDNESS spline
inline SplinePtr ts_m38857(float f) {
    return SplineBuilder(SplineAxis::WEIRDNESS)
        .add(-0.01f, 0.63f * f, 0)
        .add(0.01f, 0.3f * f, 0)
        .build();
}

// [bytecode-verified] method_38855(p0,p1): RIDGES spline gating on the two
// "getNormalizedWeirdness" thresholds
inline SplinePtr ts_m38855(float p0, float p1) {
    float w1 = getNormalizedWeirdness(0.4f);
    float w2 = getNormalizedWeirdness(0.56666666f);
    float mid = (w1 + w2) / 2.0f;
    SplineBuilder b(SplineAxis::RIDGES);
    b.add(w1, 0.0f, 0);
    if (p1 > 0.0f) b.add(mid, ts_m38857(p1), 0); else b.add(mid, 0.0f, 0);
    if (p0 > 0.0f) b.add(1.0f, ts_m38857(p0), 0); else b.add(1.0f, 0.0f, 0);
    return b.build();
}

// [bytecode-verified] method_38856(p0,p1,p2,p3): used for the peaks spline
inline SplinePtr ts_m38856(float p0, float p1, float p2, float p3) {
    SplinePtr s0 = ts_m38855(p0, p2);
    SplinePtr s1 = ts_m38855(p1, p3);
    return SplineBuilder(SplineAxis::EROSION)
        .add(-1.0f, s0, 0)
        .add(-0.78f, s1, 0)
        .add(-0.5775f, s1, 0)
        .add(-0.375f, 0.0f, 0)
        .build();
}

// [bytecode-verified] buildErosionFactorSpline(f, bl)
inline SplinePtr buildErosionFactorSpline(float f, bool bl) {
    SplinePtr s1 = SplineBuilder(SplineAxis::WEIRDNESS).add(-0.2f, 6.3f, 0).add(0.2f, f, 0).build();
    SplineBuilder b(SplineAxis::EROSION);
    b.add(-0.6f, s1, 0);
    b.add(-0.5f, SplineBuilder(SplineAxis::WEIRDNESS).add(-0.05f, 6.3f, 0).add(0.05f, 2.67f, 0).build(), 0);
    b.add(-0.35f, s1, 0);
    b.add(-0.25f, s1, 0);
    b.add(-0.1f, SplineBuilder(SplineAxis::WEIRDNESS).add(-0.05f, 2.67f, 0).add(0.05f, 6.3f, 0).build(), 0);
    b.add(0.03f, s1, 0);
    if (bl) {
        SplinePtr s3 = SplineBuilder(SplineAxis::WEIRDNESS).add(0.0f, f, 0).add(0.1f, 0.625f, 0).build();
        SplinePtr s4 = SplineBuilder(SplineAxis::RIDGES).add(-0.9f, f, 0).add(-0.69f, s3, 0).build();
        b.add(0.35f, f, 0);
        b.add(0.45f, s4, 0);
        b.add(0.55f, s4, 0);
        b.add(0.62f, f, 0);
    } else {
        SplinePtr s3 = SplineBuilder(SplineAxis::RIDGES).add(-0.7f, s1, 0).add(-0.15f, 1.37f, 0).build();
        SplinePtr s4 = SplineBuilder(SplineAxis::RIDGES).add(0.45f, s1, 0).add(0.7f, 1.56f, 0).build();
        b.add(0.05f, s4, 0);
        b.add(0.4f, s4, 0);
        b.add(0.45f, s3, 0);
        b.add(0.55f, s3, 0);
        b.add(0.58f, f, 0);
    }
    return b.build();
}

// [bytecode-verified] getOffsetValue / method_38217 / method_38219 -- the erosion-band
// "land spline" family used by createLandSpline.
inline float ts_getOffsetValue(float weirdness, float y, float z) {
    float f3 = 1.0f - (1.0f - y) * 0.5f;
    float f4 = 0.5f * (1.0f - y);
    float f5 = (weirdness + 1.17f) * 0.46082947f;
    float f6 = f5 * f3 - f4;
    if (weirdness < z) return std::fmax(f6, -0.2222f);
    return std::fmax(f6, 0.0f);
}

inline float ts_m38217(float y) {
    float f1 = 1.17f, f2 = 0.46082947f;
    float f3 = 1.0f - (1.0f - y) * 0.5f;
    float f4 = 0.5f * (1.0f - y);
    return f4 / (f2 * f3) - f1;
}

// [bytecode-verified] method_38210(a,b,x0,x1) -- linear slope between two (x,y) pairs
inline float ts_m38210(float y0, float y1, float x0, float x1) {
    return (y1 - y0) / (x1 - x0);
}

inline SplinePtr ts_m38219(float y, bool bl) {
    SplineBuilder b(SplineAxis::RIDGES);
    float f3 = ts_getOffsetValue(-1.0f, y, -0.7f);
    float f4 = ts_getOffsetValue(1.0f, y, -0.7f);
    float f5 = ts_m38217(y);
    if (-0.65f < f5 && f5 < 1.0f) {
        float f6 = ts_getOffsetValue(-0.65f, y, -0.7f);
        float f7 = ts_getOffsetValue(-0.75f, y, -0.7f);
        float f8 = ts_m38210(f3, f7, -1.0f, -0.75f);
        b.add(-1.0f, f3, f8);
        b.add(-0.75f, f7, 0);
        b.add(-0.65f, f6, 0);
        float f9 = ts_getOffsetValue(f5, y, -0.7f);
        float f10 = ts_m38210(f9, f4, f5, 1.0f);
        b.add(f5 - 0.01f, f9, 0);
        b.add(f5, f9, f10);
        b.add(1.0f, f4, f10);
    } else {
        float f6 = ts_m38210(f3, f4, -1.0f, 1.0f);
        if (bl) {
            b.add(-1.0f, std::fmax(0.2f, f3), 0);
            b.add(0.0f, ts_lerp(0.5f, f3, f4), f6);
        } else {
            b.add(-1.0f, f3, f6);
        }
        b.add(1.0f, f4, f6);
    }
    return b.build();
}

// [bytecode-verified] createFlatOffsetSpline(p0..p5)
inline SplinePtr createFlatOffsetSpline(float p0, float p1, float p2, float p3, float p4, float p5) {
    float a = std::fmax(0.5f * (p1 - p0), p5);
    float b_ = 5.0f * (p2 - p1);
    return SplineBuilder(SplineAxis::RIDGES)
        .add(-1.0f, p0, a)
        .add(-0.4f, p1, std::fmin(a, b_))
        .add(0.0f, p2, b_)
        .add(0.4f, p3, 2.0f * (p3 - p2))
        .add(1.0f, p4, 0.7f * (p4 - p3))
        .build();
}

// [bytecode-verified] createLandSpline(p0..p5, bl6, bl7)
inline SplinePtr createLandSpline(float p0, float p1, float p2, float p3, float p4, float p5, bool bl6, bool bl7) {
    SplinePtr s8 = ts_m38219(ts_lerp(p3, 0.6f, 1.5f), bl7);
    SplinePtr s9 = ts_m38219(ts_lerp(p3, 0.6f, 1.0f), bl7);
    SplinePtr s10 = ts_m38219(p3, bl7);
    SplinePtr s11 = createFlatOffsetSpline(p0 - 0.15f, 0.5f * p3, 0.5f * p3, 0.5f * p3, 0.6f * p3, 0.5f);
    SplinePtr s12 = createFlatOffsetSpline(p0, p4 * p3, p1 * p3, 0.5f * p3, 0.6f * p3, 0.5f);
    SplinePtr s13 = createFlatOffsetSpline(p0, p4, p4, p1, p2, 0.5f);
    SplinePtr s14 = createFlatOffsetSpline(p0, p4, p4, p1, p2, 0.5f);
    SplinePtr s15 = SplineBuilder(SplineAxis::RIDGES)
        .add(-1.0f, p0, 0)
        .add(-0.4f, s13, 0)
        .add(0.0f, p2 + 0.07f, 0)
        .build();
    SplinePtr s16 = createFlatOffsetSpline(-0.02f, p5, p5, p1, p2, 0.0f);
    SplineBuilder b17(SplineAxis::EROSION);
    b17.add(-0.85f, s8, 0);
    b17.add(-0.7f, s9, 0);
    b17.add(-0.4f, s10, 0);
    b17.add(-0.35f, s11, 0);
    b17.add(-0.1f, s12, 0);
    b17.add(0.2f, s13, 0);
    if (bl6) {
        b17.add(0.4f, s14, 0);
        b17.add(0.45f, s15, 0);
        b17.add(0.55f, s15, 0);
        b17.add(0.58f, s14, 0);
    }
    b17.add(0.7f, s16, 0);
    return b17.build();
}

struct VanillaTerrainParameters {
    SplinePtr offsetSpline, factorSpline, peakSpline;

    // [bytecode-verified] createSurfaceParameters()
    static VanillaTerrainParameters createSurfaceParameters() {
        VanillaTerrainParameters vtp;
        SplinePtr s0 = createLandSpline(-0.15f, 0.0f, 0.0f, 0.1f, 0.0f, -0.03f, false, false);
        SplinePtr s1 = createLandSpline(-0.1f, 0.03f, 0.1f, 0.1f, 0.01f, -0.03f, false, false);
        SplinePtr s2 = createLandSpline(-0.1f, 0.03f, 0.1f, 0.7f, 0.01f, -0.03f, true, true);
        SplinePtr s3 = createLandSpline(-0.05f, 0.03f, 0.1f, 1.0f, 0.01f, 0.01f, true, true);

        vtp.offsetSpline = SplineBuilder(SplineAxis::CONTINENTS)
            .add(-1.1f, 0.044f, 0)
            .add(-1.02f, -0.2222f, 0)
            .add(-0.51f, -0.2222f, 0)
            .add(-0.44f, -0.12f, 0)
            .add(-0.18f, -0.12f, 0)
            .add(-0.16f, s0, 0)
            .add(-0.15f, s0, 0)
            .add(-0.1f, s1, 0)
            .add(0.25f, s2, 0)
            .add(1.0f, s3, 0)
            .build();

        vtp.factorSpline = SplineBuilder(SplineAxis::CONTINENTS)
            .add(-0.19f, 3.95f, 0)
            .add(-0.15f, buildErosionFactorSpline(6.25f, true), 0)
            .add(-0.1f, buildErosionFactorSpline(5.47f, true), 0)
            .add(0.03f, buildErosionFactorSpline(5.08f, true), 0)
            .add(0.06f, buildErosionFactorSpline(4.69f, false), 0)
            .build();

        vtp.peakSpline = SplineBuilder(SplineAxis::CONTINENTS)
            .add(-0.11f, 0.0f, 0)
            .add(0.03f, ts_m38856(1.0f, 0.5f, 0.0f, 0.0f), 0)
            .add(0.65f, ts_m38856(1.0f, 1.0f, 1.0f, 0.0f), 0)
            .build();
        return vtp;
    }

    // [bytecode-verified] createNoisePoint(continentalness, erosion, weirdness)
    TS_HD static NoisePointF createNoisePoint(float continentalness, float erosion, float weirdness) {
        return NoisePointF{continentalness, erosion, getNormalizedWeirdness(weirdness), weirdness};
    }

    float getOffset(const NoisePointF& p) const { return offsetSpline->apply(p) + (-0.50375f); }
    float getFactor(const NoisePointF& p) const { return factorSpline->apply(p); }
    float getPeak(const NoisePointF& p) const { return peakSpline->apply(p); }
};

// ---------- GPU-safe flattened form ----------
// The whole spline tree is static (same for every seed/thread), so it's built once on
// the host and flattened into an index-based array -- raw pointers/shared_ptr can't
// survive a host->device memcpy, but array indices into a single uploaded blob can.
#define DEV_SPLINE_MAX_POINTS 12
#define DEV_SPLINE_MAX_NODES 256

struct DevSplineNode {
    bool isConst;
    float constValue;
    int axis; // SplineAxis
    int numPoints;
    float locations[DEV_SPLINE_MAX_POINTS];
    float derivatives[DEV_SPLINE_MAX_POINTS];
    bool isChild[DEV_SPLINE_MAX_POINTS];
    int childIndex[DEV_SPLINE_MAX_POINTS];   // valid iff isChild[i]
    float constAt[DEV_SPLINE_MAX_POINTS];    // valid iff !isChild[i]
};

struct DevSplineTable {
    DevSplineNode nodes[DEV_SPLINE_MAX_NODES];
    int count;
    int offsetRoot, factorRoot, peakRoot;
};

// Host-side flattening: walks the shared_ptr tree, memoizing by pointer identity so
// shared sub-splines (e.g. createLandSpline's reused s13/s14) aren't duplicated.
inline int flattenSpline(const SplinePtr& s, DevSplineTable& table, std::vector<const Spline*>& seen) {
    for (size_t i = 0; i < seen.size(); i++) {
        if (seen[i] == s.get()) return (int)i;
    }
    int idx = table.count++;
    if (idx >= DEV_SPLINE_MAX_NODES) { fprintf(stderr, "DEV_SPLINE_MAX_NODES exceeded\n"); abort(); }
    seen.push_back(s.get());
    DevSplineNode& n = table.nodes[idx];
    n.isConst = s->isConst;
    n.constValue = s->constValue;
    if (!s->isConst) {
        n.axis = (int)s->axis;
        n.numPoints = (int)s->locations.size();
        if (n.numPoints > DEV_SPLINE_MAX_POINTS) { fprintf(stderr, "DEV_SPLINE_MAX_POINTS exceeded\n"); abort(); }
        for (int i = 0; i < n.numPoints; i++) {
            n.locations[i] = s->locations[i];
            n.derivatives[i] = s->derivatives[i];
            if (s->values[i]->isConst) {
                n.isChild[i] = false;
                n.constAt[i] = s->values[i]->constValue;
            } else {
                n.isChild[i] = true;
                n.childIndex[i] = flattenSpline(s->values[i], table, seen);
            }
        }
    }
    return idx;
}

inline DevSplineTable flattenTerrainParameters(const VanillaTerrainParameters& vtp) {
    DevSplineTable table;
    table.count = 0;
    std::vector<const Spline*> seen;
    table.offsetRoot = flattenSpline(vtp.offsetSpline, table, seen);
    table.factorRoot = flattenSpline(vtp.factorSpline, table, seen);
    table.peakRoot = flattenSpline(vtp.peakSpline, table, seen);
    return table;
}

TS_HD inline float dev_splineApply(const DevSplineTable& table, int nodeIdx, const NoisePointF& p) {
    const DevSplineNode& n = table.nodes[nodeIdx];
    if (n.isConst) return n.constValue;
    float x = ts_coord((SplineAxis)n.axis, p);
    int last = n.numPoints - 1;
    int idx = -1;
    for (int i = 0; i <= last; i++) {
        if (n.locations[i] <= x) idx = i; else break;
    }
    auto valueAt = [&](int i) -> float {
        return n.isChild[i] ? dev_splineApply(table, n.childIndex[i], p) : n.constAt[i];
    };
    if (idx < 0) {
        return valueAt(0) + n.derivatives[0] * (x - n.locations[0]);
    }
    if (idx == last) {
        return valueAt(last) + n.derivatives[last] * (x - n.locations[last]);
    }
    float x0 = n.locations[idx], x1 = n.locations[idx + 1];
    float t = (x - x0) / (x1 - x0);
    float y0 = valueAt(idx), y1 = valueAt(idx + 1);
    float d0 = n.derivatives[idx], d1 = n.derivatives[idx + 1];
    float k0 = d0 * (x1 - x0) - (y1 - y0);
    float k1 = -d1 * (x1 - x0) + (y1 - y0);
    return ts_lerp(t, y0, y1) + t * (1.0f - t) * ts_lerp(t, k0, k1);
}
