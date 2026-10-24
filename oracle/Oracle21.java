import it.unimi.dsi.fastutil.shorts.ShortList;
import net.minecraft.SharedConstants;
import net.minecraft.core.BlockPos;
import net.minecraft.core.registries.BuiltInRegistries;
import net.minecraft.server.Bootstrap;
import net.minecraft.util.RandomSource;
import net.minecraft.world.level.ChunkPos;
import net.minecraft.world.level.WorldGenLevel;
import net.minecraft.world.level.block.Blocks;
import net.minecraft.world.level.block.state.BlockState;
import net.minecraft.world.level.chunk.ChunkAccess;
import net.minecraft.world.level.levelgen.Heightmap;
import net.minecraft.world.level.levelgen.LegacyRandomSource;
import net.minecraft.world.level.levelgen.WorldgenRandom;
import net.minecraft.world.level.levelgen.XoroshiroRandomSource;
import net.minecraft.world.level.levelgen.structure.BoundingBox;
import net.minecraft.world.level.levelgen.structure.StructurePiece;
import net.minecraft.world.level.levelgen.structure.pieces.StructurePiecesBuilder;
import net.minecraft.world.level.levelgen.structure.structures.MineshaftPieces;
import net.minecraft.world.level.levelgen.structure.structures.MineshaftStructure;

import java.io.BufferedOutputStream;
import java.io.BufferedReader;
import java.io.ByteArrayOutputStream;
import java.io.FileReader;
import java.io.InputStream;
import java.io.PrintStream;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.HashMap;
import java.util.List;
import java.util.Random;
import java.util.concurrent.atomic.AtomicInteger;
import java.util.function.Predicate;

public class Oracle21 {
    static BlockState STONE, DEEPSLATE, AIR, WATER, LAVA, BEDROCK;
    static int[] TRACE = null; static String CURPIECE = "?";
    static { String t = System.getProperty("coin.trace"); if (t != null) { String[] f = t.split(","); TRACE = new int[]{Integer.parseInt(f[0]), Integer.parseInt(f[1]), Integer.parseInt(f[2])}; } }
    static final int DUMP_YMAX = Integer.getInteger("coin.ymax", 128);
    static final boolean DEBUG = System.getProperty("coin.debug", "").length() > 0;
    static final Heightmap.Types HUT_HM = Heightmap.Types.valueOf(System.getProperty("hut.hm", "MOTION_BLOCKING_NO_LEAVES"));
    static final boolean LCG = "lcg".equals(System.getProperty("oracle.rng", "xoro"));
    static final int SH = 16 * Integer.getInteger("coin.shift", 0);
    static final int MINY = -64, MAXY = 319;
    static final String DUMP_EXE = System.getProperty("terrain.dump", "terrain_dump.exe");

    interface Terrain { BlockState at(int x, int y, int z); }

    static final class FlatTerrain implements Terrain {
        final int top;
        FlatTerrain(int top) { this.top = top; }
        public BlockState at(int x, int y, int z) { return y < MINY ? BEDROCK : y <= top ? (y < 0 ? DEEPSLATE : STONE) : AIR; }
    }

    static final class DumpTerrain implements Terrain {
        final int cx, cz, ymin, ymax, ny; final byte[] cells;
        DumpTerrain(byte[] d) {
            java.nio.ByteBuffer bb = java.nio.ByteBuffer.wrap(d).order(java.nio.ByteOrder.LITTLE_ENDIAN);
            cx = bb.getInt(); cz = bb.getInt(); ymin = bb.getInt(); ymax = bb.getInt(); ny = ymax - ymin + 1;
            cells = new byte[d.length - 16]; System.arraycopy(d, 16, cells, 0, cells.length);
        }
        public BlockState at(int x, int y, int z) {
            final int dx = x - cx * 16, dz = z - cz * 16;
            if (dx < 0 || dx > 15 || dz < 0 || dz > 15) return y <= 60 ? (y < 0 ? DEEPSLATE : STONE) : AIR;
            if (y > ymax) return AIR;
            if (y < ymin) return BEDROCK;
            switch (cells[(dx * 16 + dz) * ny + (y - ymin)]) {
                case 0: return y < 0 ? DEEPSLATE : STONE;
                case 1: return AIR;
                case 2: return WATER;
                default: return LAVA;
            }
        }
    }

    static byte[] runDump(long seed, int cx, int cz) throws Exception {
        Process p = new ProcessBuilder(DUMP_EXE, "--seed", Long.toString(seed), "--cx", Integer.toString(cx), "--cz", Integer.toString(cz), "--ymin", "-64", "--ymax", Integer.toString(DUMP_YMAX))
                .redirectError(ProcessBuilder.Redirect.INHERIT).start();
        ByteArrayOutputStream bo = new ByteArrayOutputStream();
        try (InputStream in = p.getInputStream()) { in.transferTo(bo); }
        p.waitFor();
        return bo.toByteArray();
    }

    static final class World implements InvocationHandler {
        final Terrain terrain; final long seed; final HashMap<Long, BlockState> over = new HashMap<>();
        final HashMap<String, Integer> unknown = new HashMap<>();
        final ArrayList<String> chests = new ArrayList<>();
        World(Terrain t, long seed) { terrain = t; this.seed = seed; }
        BlockState get(int x, int y, int z) { BlockState s = over.get(BlockPos.asLong(x, y, z)); return s != null ? s : terrain.at(x, y, z); }
        static final boolean WG_FROZEN = !"false".equals(System.getProperty("wg.frozen", "true"));   // the real game does not update the *_WG heightmaps during the features step (status CARVERS tracks FINAL_HEIGHTMAPS only): answered from the terrain without the pieces; -Dwg.frozen=false = the old live rule
        int topYBase(Heightmap.Types type, int x, int z) {
            final Predicate<BlockState> pr = type.isOpaque();
            for (int y = 127; y >= MINY; y--) if (pr.test(terrain.at(x, y, z))) return y + 1;
            return MINY;
        }
        int topY(Heightmap.Types type, int x, int z) {
            final Predicate<BlockState> pr = type.isOpaque();
            for (int y = 127; y >= MINY; y--) if (pr.test(get(x, y, z))) return y + 1;
            return MINY;
        }
        public Object invoke(Object proxy, Method m, Object[] a) throws Throwable {
            switch (m.getName()) {
                case "getBlockState": { BlockPos p = (BlockPos) a[0]; return get(p.getX(), p.getY(), p.getZ()); }
                case "setBlock": { BlockPos p = (BlockPos) a[0]; if (TRACE != null && p.getX() == TRACE[0] && p.getY() == TRACE[1] && p.getZ() == TRACE[2]) System.out.println("OTRACE setBlock " + BuiltInRegistries.BLOCK.getKey(((BlockState) a[1]).getBlock()).getPath() + " (piece " + CURPIECE + ")"); over.put(p.asLong(), (BlockState) a[1]); return Boolean.TRUE; }
                case "getFluidState": { BlockPos p = (BlockPos) a[0]; return get(p.getX(), p.getY(), p.getZ()).getFluidState(); }
                case "isEmptyBlock": { BlockPos p = (BlockPos) a[0]; return get(p.getX(), p.getY(), p.getZ()).isAir(); }
                case "getChunk": return chunkStub();
                case "getBlockEntity": return null;
                case "addFreshEntity": case "addFreshEntityWithPassengers": {
                    try { net.minecraft.world.entity.Entity e = (net.minecraft.world.entity.Entity) a[0]; chests.add(String.format("chest-minecart@(%d,%d,%d)", (int) Math.floor(e.getX()), (int) Math.floor(e.getY()), (int) Math.floor(e.getZ()))); } catch (Throwable t) { chests.add("chest-minecart"); }
                    return Boolean.TRUE; }
                case "getLevel": return levelStub();
                case "getBiome": case "getNoiseBiome": return biomeStub();
                case "getBiomeManager": return null;
                case "getHeight":
                    if (a != null && a.length == 3) { final Heightmap.Types ht = (Heightmap.Types) a[0]; int hh = (WG_FROZEN && (ht == Heightmap.Types.OCEAN_FLOOR_WG || ht == Heightmap.Types.WORLD_SURFACE_WG)) ? topYBase(ht, (Integer) a[1], (Integer) a[2]) : topY(ht, (Integer) a[1], (Integer) a[2]); if (TRACE != null && (Integer) a[1] == TRACE[0] && (Integer) a[2] == TRACE[2]) System.out.println("OTRACE getHeight " + a[0] + " = " + hh + " (piece " + CURPIECE + ")"); return hh; }
                    return MAXY - MINY + 1;
                case "getMinY": case "getMinBuildHeight": return MINY;
                case "getMaxY": return MAXY;
                case "getMaxBuildHeight": return MAXY + 1;
                case "getSectionsCount": return (MAXY - MINY + 1) / 16;
                case "getSeaLevel": return 63;
                case "getSeed": return seed;
                case "isStateAtPosition": { BlockPos p = (BlockPos) a[0]; @SuppressWarnings("unchecked") Predicate<BlockState> pr = (Predicate<BlockState>) a[1]; return pr.test(get(p.getX(), p.getY(), p.getZ())); }
                case "ensureCanWrite": case "hasChunk": case "isLoaded": case "hasChunkAt": case "isInsideBuildHeight": return Boolean.TRUE;
                case "setCurrentlyGenerating": return null;
                case "hashCode": return System.identityHashCode(proxy);
                case "equals": return proxy == a[0];
                case "toString": return "OracleWorld";
                default:
                    unknown.merge(m.getName(), 1, Integer::sum);
                    if (m.isDefault()) return InvocationHandler.invokeDefault(proxy, m, a);
                    Class<?> r = m.getReturnType();
                    if (r == boolean.class) return false;
                    if (r == int.class) return 0;
                    if (r == long.class) return 0L;
                    if (r == double.class) return 0.0;
                    if (r == float.class) return 0f;
                    return null;
            }
        }
    }

    private static Object LEVEL;

    static synchronized Object levelStub() throws Exception {
        if (LEVEL == null) {
            Field uf = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe"); uf.setAccessible(true);
            sun.misc.Unsafe u = (sun.misc.Unsafe) uf.get(null);
            Object sl = u.allocateInstance(net.minecraft.server.level.ServerLevel.class);
            Object srv = u.allocateInstance(net.minecraft.server.dedicated.DedicatedServer.class);
            Object flags; try { flags = net.minecraft.world.flag.FeatureFlags.class.getField("VANILLA_SET").get(null); } catch (NoSuchFieldException e) { flags = net.minecraft.world.flag.FeatureFlags.class.getField("DEFAULT_FLAGS").get(null); }
            final Object ff = flags;
            Object wd = Proxy.newProxyInstance(Oracle21.class.getClassLoader(), new Class<?>[]{net.minecraft.world.level.storage.WorldData.class}, (pr, m, a) -> m.getName().equals("enabledFeatures") ? ff : (m.getName().equals("hashCode") ? 5 : (m.getName().equals("equals") ? Boolean.valueOf(pr == a[0]) : null)));
            Field wf = net.minecraft.server.MinecraftServer.class.getDeclaredField("worldData"); u.putObject(srv, u.objectFieldOffset(wf), wd);
            Field sf = net.minecraft.server.level.ServerLevel.class.getDeclaredField("server"); u.putObject(sl, u.objectFieldOffset(sf), srv);
            LEVEL = sl;
        }
        return LEVEL;
    }

    private static Object BIOME;

    static synchronized Object biomeStub() {
        if (BIOME == null) {
            BIOME = Proxy.newProxyInstance(Oracle21.class.getClassLoader(), new Class<?>[]{net.minecraft.core.Holder.class}, (pr, m, a) -> {
                switch (m.getName()) {
                    case "hashCode": return 11;
                    case "equals": return pr == a[0];
                    case "toString": return "BiomeStub";
                    default:
                        Class<?> r = m.getReturnType();
                        if (r == boolean.class) return false;
                        if (r == int.class) return 0;
                        return null;
                }
            });
        }
        return BIOME;
    }

    private static ChunkAccess CHUNK;

    static synchronized ChunkAccess chunkStub() throws Exception {
        if (CHUNK == null) {
            Field uf = Class.forName("sun.misc.Unsafe").getDeclaredField("theUnsafe"); uf.setAccessible(true);
            sun.misc.Unsafe u = (sun.misc.Unsafe) uf.get(null);
            ChunkAccess c = (ChunkAccess) u.allocateInstance(net.minecraft.world.level.chunk.ProtoChunk.class);
            net.minecraft.world.level.LevelHeightAccessor hl = (net.minecraft.world.level.LevelHeightAccessor) Proxy.newProxyInstance(Oracle21.class.getClassLoader(), new Class<?>[]{net.minecraft.world.level.LevelHeightAccessor.class}, (pr, m, a) -> {
                switch (m.getName()) {
                    case "getMinY": case "getMinBuildHeight": return MINY;
                    case "getMaxY": return MAXY;
                    case "getMaxBuildHeight": return MAXY + 1;
                    case "getHeight": return MAXY - MINY + 1;
                    case "hashCode": return 7;
                    case "equals": return pr == a[0];
                    default: if (m.isDefault()) return InvocationHandler.invokeDefault(pr, m, a); return null;
                }
            });
            Field f1 = ChunkAccess.class.getDeclaredField("postProcessing"); f1.setAccessible(true); f1.set(c, new ShortList[(MAXY - MINY + 1) / 16]);
            Field f2 = ChunkAccess.class.getDeclaredField("levelHeightAccessor"); f2.setAccessible(true); f2.set(c, hl);
            CHUNK = c;
        }
        return CHUNK;
    }

    static final class Result { int air, hutY, columnsOpen, roomBlocks = -1; long draws; String note = ""; ArrayList<String> chests = new ArrayList<>(); int spawners, cobwebs, roofCells, roofOpen, rails; }

    static List<StructurePiece> pieces(long W, int cx, int cz) {
        WorldgenRandom random = new WorldgenRandom(new LegacyRandomSource(0L));
        random.setLargeFeatureSeed(W, cx, cz);
        random.nextDouble();
        MineshaftPieces.MineShaftRoom room = new MineshaftPieces.MineShaftRoom(0, random, cx * 16 + SH + 2, cz * 16 + SH + 2, MineshaftStructure.Type.NORMAL);
        StructurePiecesBuilder b = new StructurePiecesBuilder();
        b.addPiece(room);
        room.addChildren(room, b, random);
        b.moveBelowSeaLevel(63, MINY, random, 10);
        return b.build().pieces();
    }

    static WorldgenRandom decorationRandom(long W, int cx, int cz, int index) {
        if (LCG) {
            Random r = new Random(W); long l = r.nextLong() | 1L, m = r.nextLong() | 1L;
            long pop = ((long) (cx * 16) * l + (long) (cz * 16) * m) ^ W;
            WorldgenRandom wr = new WorldgenRandom(new LegacyRandomSource(0L)); wr.setSeed(pop + index + 10000L * 3); return wr;
        }
        WorldgenRandom wr = new WorldgenRandom(new XoroshiroRandomSource(0L));
        long dec = wr.setDecorationSeed(W, cx * 16, cz * 16);
        wr.setFeatureSeed(dec, index, 3);
        return wr;
    }

    static Result run(long W, int cx, int cz, int index, Terrain terrain, List<StructurePiece> pcs) {
        final World w = new World(terrain, W);
        final WorldGenLevel level = (WorldGenLevel) Proxy.newProxyInstance(Oracle21.class.getClassLoader(), new Class<?>[]{WorldGenLevel.class}, w);
        final WorldgenRandom random = decorationRandom(W, cx, cz, index);
        final int gx = cx * 16 + SH, gz = cz * 16 + SH;
        final BoundingBox chunkBox = new BoundingBox(gx, MINY + 1, gz, gx + 15, MAXY, gz + 15);
        final ChunkPos cpos = new ChunkPos(cx + SH / 16, cz + SH / 16);
        final BoundingBox first = pcs.get(0).getBoundingBox();
        final BlockPos center = first.getCenter();
        final BlockPos pivot = new BlockPos(center.getX(), first.minY(), center.getZ());
        Result r = new Result();
        for (StructurePiece p : pcs) {
            if (!p.getBoundingBox().intersects(chunkBox)) continue;
            final int c0 = random.getCount(); final int o0 = w.over.size();
            CURPIECE = p.getClass().getSimpleName() + " " + p.getBoundingBox();
            try { p.postProcess(level, null, null, random, chunkBox, cpos, pivot); if (r.roomBlocks < 0) r.roomBlocks = w.over.size() - o0; }
            catch (Throwable t) { r.note += " postProcess threw " + t; if (System.getProperty("coin.stack") != null) { synchronized (System.out) { t.printStackTrace(System.out); } } }
            if (DEBUG) System.out.printf("    %-22s %s  draws +%d  blocks +%d%n", p.getClass().getSimpleName(), p.getBoundingBox(), random.getCount() - c0, w.over.size() - o0);
        }
        r.draws = random.getCount();
        if (System.getProperty("coin.final") != null) {
            StringBuilder fb = new StringBuilder();
            for (int x = gx; x < gx + 16; x++) for (int z = gz; z < gz + 16; z++) {
                fb.append("FIN ").append(W).append(' ').append(x).append(' ').append(z).append(' ');
                for (int y = -64; y <= 12; y++) fb.append(w.get(x, y, z).isAir() ? '0' : '1');
                fb.append('\n');
            }
            for (java.util.Map.Entry<Long, BlockState> e : w.over.entrySet()) {
                BlockPos bp = BlockPos.of(e.getKey()); BlockState bs = e.getValue();
                if (bs.isAir() || bp.getY() > 12 || bp.getX() < gx || bp.getX() >= gx + 16 || bp.getZ() < gz || bp.getZ() >= gz + 16) continue;
                fb.append("NAME ").append(W).append(' ').append(bp.getX()).append(' ').append(bp.getY()).append(' ').append(bp.getZ()).append(' ').append(BuiltInRegistries.BLOCK.getKey(bs.getBlock()).getPath()).append('\n');
            }
            synchronized (System.out) { System.out.print(fb); System.out.flush(); }
        }
        long hut = 0; int air = 0, open = 0;
        for (int dz = 0; dz < 9; dz++) for (int dx = 0; dx < 7; dx++) {
            final int x = gx + dx, z = gz + dz;
            hut += w.topY(HUT_HM, x, z);   // the game reads MOTION_BLOCKING_NO_LEAVES (ScatteredFeaturePiece bytecode); -Dhut.hm=WORLD_SURFACE_WG = the old rule
            int d = 0;
            for (int y = -55; y >= -63; y--) { if (!w.get(x, y, z).isAir()) break; d++; }
            air += d; if (d > 0) open++;
        }
        if (Integer.getInteger("coin.ylayer", Integer.MIN_VALUE) != Integer.MIN_VALUE) {
            final int yl = Integer.getInteger("coin.ylayer"); StringBuilder sb = new StringBuilder("    layer y=" + yl + " (rows z 0..8, cols x 0..6; A air, else first letter of the block):\n");
            for (int dz = 0; dz < 9; dz++) { sb.append("      "); for (int dx = 0; dx < 7; dx++) { BlockState st = w.get(gx + dx, yl, gz + dz); String nm = BuiltInRegistries.BLOCK.getKey(st.getBlock()).getPath(); sb.append(st.isAir() ? 'A' : Character.toUpperCase(nm.charAt(0))).append(' '); } sb.append('\n'); }
            System.out.print(sb);
        }
        r.hutY = (int) (hut / 63); r.air = air; r.columnsOpen = open; r.chests = w.chests;
        for (BlockState st : w.over.values()) { if (st.is(Blocks.SPAWNER)) r.spawners++; else if (st.is(Blocks.COBWEB)) r.cobwebs++; else if (st.is(Blocks.RAIL)) r.rails++; }
        for (StructurePiece p : pcs) {
            if (!(p instanceof MineshaftPieces.MineShaftCorridor)) continue;
            BoundingBox b = p.getBoundingBox();
            if (b.maxX() < gx || b.minX() > gx + 15 || b.maxZ() < gz || b.minZ() > gz + 15) continue;
            final int roofY = b.minY() + 2;
            for (int x = Math.max(b.minX(), gx); x <= Math.min(b.maxX(), gx + 15); x++)
                for (int z = Math.max(b.minZ(), gz); z <= Math.min(b.maxZ(), gz + 15); z++) { r.roofCells++; if (w.get(x, roofY, z).isAir()) r.roofOpen++; }
        }
        if (!w.unknown.isEmpty()) r.note += " unknown " + w.unknown;
        return r;
    }

    static int reading(int air) { return (int) ((-54L * 63 - air) / 63); }

    static int neighbourStarts(long W, int cx, int cz, StringBuilder nbs) {
        final Random ab = new Random(W); final long a = ab.nextLong(), b = ab.nextLong(); int nb = 0;
        for (int dx = -7; dx <= 7; dx++) for (int dz = -7; dz <= 7; dz++) {
            if (dx == 0 && dz == 0) continue;
            final long carver = ((long) (cx + dx) * a ^ (long) (cz + dz) * b) ^ W;
            if (new Random(carver).nextDouble() < 0.004) { nb++; if (nbs != null) nbs.append(String.format(" (%d,%d)", cx + dx, cz + dz)); }
        }
        return nb;
    }

    static List<long[]> readSpots(String file) throws Exception {
        final List<long[]> spots = new ArrayList<>();
        try (BufferedReader in = new BufferedReader(new FileReader(file))) {
            String ln;
            while ((ln = in.readLine()) != null) { ln = ln.trim(); if (ln.isEmpty() || ln.startsWith("#")) continue; String[] f = ln.split("[\\s,]+"); spots.add(new long[]{Long.parseLong(f[0]), Long.parseLong(f[1]), Long.parseLong(f[2])}); }
        }
        return spots;
    }

    interface Job { void run(long W, int cx, int cz, PrintStream out) throws Exception; }

    static void parallel(List<long[]> spots, int nth, PrintStream out, Job job) throws Exception {
        final AtomicInteger next = new AtomicInteger();
        final Thread[] ts = new Thread[nth];
        for (int t = 0; t < nth; t++) {
            ts[t] = new Thread(() -> {
                int i;
                while ((i = next.getAndIncrement()) < spots.size()) {
                    final long[] s = spots.get(i);
                    try { job.run(s[0], (int) s[1], (int) s[2], out); }
                    catch (Throwable e) { synchronized (out) { out.printf("# error %d %d %d %s%n", s[0], s[1], s[2], e); } }
                }
            });
            ts[t].start();
        }
        for (Thread t : ts) t.join();
        out.flush();
    }

    public static void main(String[] args) throws Exception {
        SharedConstants.tryDetectVersion();
        Bootstrap.bootStrap();
        STONE = Blocks.STONE.defaultBlockState(); DEEPSLATE = Blocks.DEEPSLATE.defaultBlockState(); AIR = Blocks.AIR.defaultBlockState();
        WATER = Blocks.WATER.defaultBlockState(); LAVA = Blocks.LAVA.defaultBlockState(); BEDROCK = Blocks.BEDROCK.defaultBlockState();
        final String mode = args.length > 0 ? args[0] : "";
        final PrintStream out = new PrintStream(new BufferedOutputStream(System.out, 1 << 16), false);
        if (mode.equals("one")) {
            final long W = Long.parseLong(args[1]); final int cx = Integer.parseInt(args[2]), cz = Integer.parseInt(args[3]);
            final Terrain terrain = args[5].equals("flat") ? new FlatTerrain(70) : new DumpTerrain(runDump(W, cx, cz));
            final List<StructurePiece> pcs = pieces(W, cx, cz);
            System.out.printf("seed %d chunk (%d,%d): %d pieces, terrain %s, decoration stream %s%n", W, cx, cz, pcs.size(), args[5], LCG ? "LCG (reference)" : "Xoroshiro (game)");
            for (String is : args[4].split(",")) {
                final int idx = Integer.parseInt(is);
                final Result r = run(W, cx, cz, idx, terrain, pcs);
                System.out.printf("  index %d: draws %d  hutY %d  air(-55 down) %d  columnsOpen %d  reading(air) %d  room blocks placed %d (0 = room refused by liquid)%s%n", idx, r.draws, r.hutY, r.air, r.columnsOpen, reading(r.air), r.roomBlocks, r.note);
            }
            return;
        }
        if (mode.equals("screen") || mode.equals("batchmt")) {
            final int idx = Integer.parseInt(args[1]); final int minAir = Integer.parseInt(args[3]); final int nth = Integer.parseInt(args[4]);
            final boolean flat54 = mode.equals("screen") || (args.length > 5 && args[5].equals("flat54"));
            final List<long[]> spots = readSpots(args[2]); final AtomicInteger good = new AtomicInteger();
            parallel(spots, nth, out, (W, cx, cz, o) -> {
                final List<StructurePiece> pcs = pieces(W, cx, cz);
                final Terrain terrain = flat54 ? new FlatTerrain(-55) : new DumpTerrain(runDump(W, cx, cz));
                final Result r = run(W, cx, cz, idx, terrain, pcs);
                if (r.air < minAir) return;
                final StringBuilder nbs = new StringBuilder(); final int nb = neighbourStarts(W, cx, cz, nbs);
                good.incrementAndGet();
                synchronized (o) {
                    if (mode.equals("screen")) o.printf("%d %d %d %d %d%n", W, cx, cz, r.air, nb);
                    else o.printf("S %d chunk %d %d  air %d open %d hutY %d draws %d  chests %d spawners %d cobwebs %d rails %d roof %d/%d  neighborStarts %d%s room+%d%s%n",
                            W, cx, cz, r.air, r.columnsOpen, r.hutY, r.draws, r.chests.size(), r.spawners, r.cobwebs, r.rails, r.roofOpen, r.roofCells, nb, nbs, r.roomBlocks, r.note);
                }
            });
            System.err.printf("%s: %d spots, %d with air >= %d%n", mode, spots.size(), good.get(), minAir);
            return;
        }
        System.err.println("usage: one W cx cz idx[,idx] flat|dump | screen idx file minAir threads | batchmt idx file minAir threads [flat54]");
    }
}
