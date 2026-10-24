#pragma once
#include "mineshaft_tree.h"

struct MtFrame {
    int32_t x0, y0, z0, x1, y1, z1;
    int8_t type, facing, depth, phase;
    int32_t k, side, jj;
#ifdef MT_RECORD_META
    int32_t pidx;
#endif
};
#define MT_MAXF 16

struct MtReq { int i, j, k, dir, l; };

MT_HD inline MtReq mtReq(int i, int j, int k, int dir, int l) { MtReq r; r.i = i; r.j = j; r.k = k; r.dir = dir; r.l = l; return r; }

MT_HD inline bool mtNextChild(MtTree& t, MtFrame& f, MtReq* r) {
    switch (f.type) {
    case MT_STAIRS:
        if (f.phase) return false;
        f.phase = 1;
        switch (f.facing) {
        default:
        case MT_N: *r = mtReq(f.x0, f.y0, f.z0 - 1, MT_N, f.depth); break;
        case MT_S: *r = mtReq(f.x0, f.y0, f.z1 + 1, MT_S, f.depth); break;
        case MT_W: *r = mtReq(f.x0 - 1, f.y0, f.z0, MT_W, f.depth); break;
        case MT_E: *r = mtReq(f.x1 + 1, f.y0, f.z0, MT_E, f.depth); break;
        }
        return true;

    case MT_CROSSING: {
        const int d = f.depth;
        while (f.phase < 7) {
            const int ph = f.phase++;
            if (ph < 3) {
                int i, k, dir;
                switch (f.facing) {
                default:
                case MT_N:
                    if (ph == 0)      { i = f.x0 + 1; k = f.z0 - 1; dir = MT_N; }
                    else if (ph == 1) { i = f.x0 - 1; k = f.z0 + 1; dir = MT_W; }
                    else              { i = f.x1 + 1; k = f.z0 + 1; dir = MT_E; }
                    break;
                case MT_S:
                    if (ph == 0)      { i = f.x0 + 1; k = f.z1 + 1; dir = MT_S; }
                    else if (ph == 1) { i = f.x0 - 1; k = f.z0 + 1; dir = MT_W; }
                    else              { i = f.x1 + 1; k = f.z0 + 1; dir = MT_E; }
                    break;
                case MT_W:
                    if (ph == 0)      { i = f.x0 + 1; k = f.z0 - 1; dir = MT_N; }
                    else if (ph == 1) { i = f.x0 + 1; k = f.z1 + 1; dir = MT_S; }
                    else              { i = f.x0 - 1; k = f.z0 + 1; dir = MT_W; }
                    break;
                case MT_E:
                    if (ph == 0)      { i = f.x0 + 1; k = f.z0 - 1; dir = MT_N; }
                    else if (ph == 1) { i = f.x0 + 1; k = f.z1 + 1; dir = MT_S; }
                    else              { i = f.x1 + 1; k = f.z0 + 1; dir = MT_E; }
                    break;
                }
                *r = mtReq(i, f.y0, k, dir, d);
                return true;
            }
            if (f.y1 - f.y0 + 1 <= 3) { f.phase = 7; return false; }
            if (!mtNextBool(t)) continue;
            const int q = ph - 3;
            if (q == 0)      *r = mtReq(f.x0 + 1, f.y0 + 3 + 1, f.z0 - 1, MT_N, d);
            else if (q == 1) *r = mtReq(f.x0 - 1, f.y0 + 3 + 1, f.z0 + 1, MT_W, d);
            else if (q == 2) *r = mtReq(f.x1 + 1, f.y0 + 3 + 1, f.z0 + 1, MT_E, d);
            else             *r = mtReq(f.x0 + 1, f.y0 + 3 + 1, f.z1 + 1, MT_S, d);
            return true;
        }
        return false;
    }

    case MT_CORRIDOR: {
        const bool ew = (f.facing != MT_N && f.facing != MT_S);
        if (f.phase == 0) {
            const int j = mtNextInt(t, 4);
            const int y = f.y0 - 1 + mtNextInt(t, 3);
            int i, k, dir;
            switch (f.facing) {
            default:
            case MT_N:
                if (j <= 1)      { i = f.x0;     k = f.z0 - 1; dir = f.facing; }
                else if (j == 2) { i = f.x0 - 1; k = f.z0;     dir = MT_W; }
                else             { i = f.x1 + 1; k = f.z0;     dir = MT_E; }
                break;
            case MT_S:
                if (j <= 1)      { i = f.x0;     k = f.z1 + 1; dir = f.facing; }
                else if (j == 2) { i = f.x0 - 1; k = f.z1 - 3; dir = MT_W; }
                else             { i = f.x1 + 1; k = f.z1 - 3; dir = MT_E; }
                break;
            case MT_W:
                if (j <= 1)      { i = f.x0 - 1; k = f.z0;     dir = f.facing; }
                else if (j == 2) { i = f.x0;     k = f.z0 - 1; dir = MT_N; }
                else             { i = f.x0;     k = f.z1 + 1; dir = MT_S; }
                break;
            case MT_E:
                if (j <= 1)      { i = f.x1 + 1; k = f.z0;     dir = f.facing; }
                else if (j == 2) { i = f.x1 - 3; k = f.z0 - 1; dir = MT_N; }
                else             { i = f.x1 - 3; k = f.z1 + 1; dir = MT_S; }
                break;
            }
            *r = mtReq(i, y, k, dir, f.depth);
            f.phase = 1;
            return true;
        }
        if (f.phase == 1) {
            if (f.depth >= 8) { f.phase = 9; return false; }
            f.k = ew ? f.x0 + 3 : f.z0 + 3;
        } else if (f.phase == 2) {
            f.k += 5;
        } else {
            return false;
        }
        const int lim = ew ? f.x1 : f.z1;
        while (f.k + 3 <= lim) {
            const int l = mtNextInt(t, 5);
            if (l == 0 || l == 1) {
                if (ew) *r = (l == 0) ? mtReq(f.k, f.y0, f.z0 - 1, MT_N, f.depth + 1)
                                      : mtReq(f.k, f.y0, f.z1 + 1, MT_S, f.depth + 1);
                else    *r = (l == 0) ? mtReq(f.x0 - 1, f.y0, f.k, MT_W, f.depth + 1)
                                      : mtReq(f.x1 + 1, f.y0, f.k, MT_E, f.depth + 1);
                f.phase = 2;
                return true;
            }
            f.k += 5;
        }
        f.phase = 9;
        return false;
    }

    default: {
        const int xs = f.x1 - f.x0 + 1, zs = f.z1 - f.z0 + 1;
        while (f.side < 4) {
            const int span = f.side < 2 ? xs : zs;
            if (f.phase == 0) f.k = 0; else f.k += 4;
            if (f.k >= span) { f.side++; f.phase = 0; continue; }
            f.k += mtNextInt(t, span);
            if (f.k + 3 > span) { f.side++; f.phase = 0; continue; }
            const int y = f.y0 + mtNextInt(t, f.jj) + 1;
            switch (f.side) {
            case 0:  *r = mtReq(f.x0 + f.k, y, f.z0 - 1, MT_N, f.depth); break;
            case 1:  *r = mtReq(f.x0 + f.k, y, f.z1 + 1, MT_S, f.depth); break;
            case 2:  *r = mtReq(f.x0 - 1, y, f.z0 + f.k, MT_W, f.depth); break;
            default: *r = mtReq(f.x1 + 1, y, f.z0 + f.k, MT_E, f.depth); break;
            }
            f.phase = 1;
            return true;
        }
        return false;
    }
    }
}

MT_HD inline void mtFrameOf(const MtPiece& p, MtFrame* f) {
    f->x0 = p.x0; f->y0 = p.y0; f->z0 = p.z0; f->x1 = p.x1; f->y1 = p.y1; f->z1 = p.z1;
    f->type = p.type; f->facing = p.facing; f->depth = p.depth; f->phase = 0;
    f->k = 0; f->side = 0;
    const int j = (p.y1 - p.y0 + 1) - 3 - 1;
    f->jj = j <= 0 ? 1 : j;
}

MT_HD inline bool mtAttempt(MtTree& t, const MtReq& q, MtPiece* np) {
    if (!(q.l <= 8 && mtAbs(q.i - t.sx) <= 80 && mtAbs(q.k - t.sz) <= 80)) return false;
    const int i = q.i, j = q.j, k = q.k, dir = q.dir;
    np->depth = (int8_t)(q.l + 1); np->rails = 0;
    np->facing = (int8_t)dir;
    const int m = mtNextInt(t, 100);
    int x0 = i, y0 = j, z0 = k, x1 = i, y1 = j + 3 - 1, z1 = k;
    if (m >= 80) {
        np->type = MT_CROSSING;
        if (mtNextInt(t, 4) == 0) y1 += 4;
        switch (dir) {
        default:
        case MT_N: x0 = i - 1; x1 = i + 3; z0 = k - 4; break;
        case MT_S: x0 = i - 1; x1 = i + 3; z1 = k + 3 + 1; break;
        case MT_W: x0 = i - 4; z0 = k - 1; z1 = k + 3; break;
        case MT_E: x1 = i + 3 + 1; z0 = k - 1; z1 = k + 3; break;
        }
        if (mtOverlaps(t, x0, y0, z0, x1, y1, z1)) return false;
    } else if (m >= 70) {
        np->type = MT_STAIRS;
        y0 = j - 5;
        switch (dir) {
        default:
        case MT_N: x1 = i + 3 - 1; z0 = k - 8; break;
        case MT_S: x1 = i + 3 - 1; z1 = k + 8; break;
        case MT_W: x0 = i - 8; z1 = k + 3 - 1; break;
        case MT_E: x1 = i + 8; z1 = k + 3 - 1; break;
        }
        if (mtOverlaps(t, x0, y0, z0, x1, y1, z1)) return false;
    } else {
        np->type = MT_CORRIDOR;
        int l2;
        for (l2 = mtNextInt(t, 3) + 2; l2 > 0; --l2) {
            const int mm = l2 * 5;
            switch (dir) {
            default:
            case MT_N: x1 = i + 3 - 1; z0 = k - (mm - 1); break;
            case MT_S: x1 = i + 3 - 1; z1 = k + mm - 1; break;
            case MT_W: x0 = i - (mm - 1); z1 = k + 3 - 1; break;
            case MT_E: x1 = i + mm - 1; z1 = k + 3 - 1; break;
            }
            if (!mtOverlaps(t, x0, y0, z0, x1, y1, z1)) break;
        }
        if (l2 <= 0) return false;
        const bool hasRails = mtNextInt(t, 3) == 0;
        np->rails = hasRails;
        if (!hasRails) (void)mtNextInt(t, 23);
    }
    np->x0 = x0; np->y0 = y0; np->z0 = z0; np->x1 = x1; np->y1 = y1; np->z1 = z1;
    return true;
}

struct MtRun {
    MtFrame st[MT_MAXF];
    int sp;
};

MT_HD inline void mtRunInit(MtTree& t, MtRun& run, int cx, int cz, int abortBelow, int budget) {
    t.n = 0; t.aborted = 0; t.overflow = 0; t.deferred = 0; t.abortBelow = abortBelow; t.budget = budget;
    t.rangeBad = 0; t.minY = 0x7fffffff; t.maxY = -0x7fffffff - 1;
    (void)mtNext(t, 26);
    (void)mtNext(t, 27);

    MtPiece room;
    room.type = MT_ROOM; room.facing = -1; room.depth = 0;
    room.x0 = cx * 16 + 2;
    room.y0 = 50;
    room.z0 = cz * 16 + 2;
    room.x1 = room.x0 + 7 + mtNextInt(t, 6);
    room.y1 = 54 + mtNextInt(t, 6);
    room.z1 = room.z0 + 7 + mtNextInt(t, 6);
    t.sx = room.x0; t.sz = room.z0;
    mtPush(t, room);
    if (room.y1 < t.minRoomTop) t.aborted = 1;

    run.sp = 0;
    mtFrameOf(room, &run.st[run.sp++]);
#ifdef MT_RECORD_META
    run.st[0].pidx = 0; t.meta[0] = (uint8_t)(MT_ROOM | (0 << 2)); t.parent[0] = -1;
#endif
}

MT_HD inline bool mtRunStep(MtTree& t, MtRun& run) {
    if (t.aborted) return false;
    MtFrame& f = run.st[run.sp - 1];
    MtReq q;
    if (!mtNextChild(t, f, &q)) { --run.sp; return run.sp > 0; }
    MtPiece np; np.rails = 0;
    if (!mtAttempt(t, q, &np)) return true;
    if (t.n >= t.budget) { t.deferred = 1; t.aborted = 1; return false; }
    if (t.n >= MT_MAXP) { t.overflow = 1; t.aborted = 1; return false; }
    mtPush(t, np);
#ifdef MT_RECORD_META
    t.meta[t.n - 1] = (uint8_t)(np.type | ((np.facing + 1) << 2) | (np.rails << 5)); t.parent[t.n - 1] = (int16_t)f.pidx;
#endif
    if (np.y0 < t.abortBelow) { t.aborted = 1; return false; }
    if (run.sp < MT_MAXF) {
        mtFrameOf(np, &run.st[run.sp]);
#ifdef MT_RECORD_META
        run.st[run.sp].pidx = t.n - 1;
#endif
        run.sp++;
        return true;
    }
    t.overflow = 1; t.aborted = 1; return false;
}

MT_HD inline void mtRunFinish(MtTree& t, MtResult* out) {
    if (t.deferred) { out->status = 3; return; }
    if (t.overflow || t.rangeBad) { out->status = 2; return; }
    if (t.aborted)  { out->status = 1; return; }

    const int minY = t.minY, maxY = t.maxY;
    const int span = maxY - minY + 1;
    int l = span + (-64) + 1;
    if (l < 53) l += mtNextInt(t, 53 - l);
    const int m = l - maxY;
    out->status = 0;
    out->pieces = t.n;
    out->spanY = span;
    out->boxMinY = minY + m;
    out->boxMaxY = maxY + m;
    out->roomFloor = 50 + m;
}

MT_HD inline void mtGenerateIter(MtTree& t, int cx, int cz, int abortBelow, MtResult* out,
                                 int budget = 0x7fffffff) {
    MtRun run;
    mtRunInit(t, run, cx, cz, abortBelow, budget);
    while (mtRunStep(t, run)) {}
    mtRunFinish(t, out);
}
