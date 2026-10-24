import net.minecraft.core.Holder;
import net.minecraft.world.level.biome.*;
import java.io.*;
import java.lang.reflect.*;
import java.util.*;

/** Writes the game's own biome search tree as text (see gen_tree.py): one line per node, depth first. */
public class TD {
    static Object get(Object o, String f) throws Exception {
        Class<?> c = o.getClass();
        while (c != null) { try { Field x = c.getDeclaredField(f); x.setAccessible(true); return x.get(o); } catch (NoSuchFieldException e) { c = c.getSuperclass(); } }
        throw new NoSuchFieldException(f);
    }
    /** Older versions: look for the parameter list inside the biome source. */
    static Object findList(Object o, int depth, Set<Object> seen) throws Exception {
        if (o == null || depth > 3 || !seen.add(o)) return null;
        if (o.getClass().getName().endsWith("Climate$ParameterList")) return o;
        Class<?> c = o.getClass();
        while (c != null && c != Object.class) {
            for (Field f : c.getDeclaredFields()) {
                if (Modifier.isStatic(f.getModifiers()) || f.getType().isPrimitive()) continue;
                f.setAccessible(true);
                Object v = f.get(o);
                if (v instanceof com.mojang.datafixers.util.Either<?, ?> ei) {
                    for (Object e : new Object[]{ei.left(), ei.right()}) if (e instanceof Optional<?> op && op.isPresent()) { Object q = findList(op.get(), depth + 1, seen); if (q != null) return q; }
                } else if (v != null && (v.getClass().getName().contains(".biome."))) {
                    Object q = findList(v, depth + 1, seen); if (q != null) return q;
                }
            }
            c = c.getSuperclass();
        }
        return null;
    }
    static PrintStream out;
    static void walk(Object n) throws Exception {
        Climate.Parameter[] ps = (Climate.Parameter[]) get(n, "parameterSpace");
        StringBuilder sb = new StringBuilder();
        for (Climate.Parameter p : ps) sb.append(' ').append(p.min()).append(' ').append(p.max());
        if (n.getClass().getSimpleName().equals("Leaf")) {
            Holder<?> h = (Holder<?>) get(n, "value");
            String name = h.unwrapKey().get().toString();
            name = name.substring(name.lastIndexOf(':') + 1).replace("]", "").trim();
            out.println("L " + name + sb);
        } else {
            Object[] ch = (Object[]) get(n, "children");
            out.println("I " + ch.length + sb);
            for (Object c : ch) walk(c);
        }
    }
    public static void dumpList(Object pl, String path) throws Exception {
        out = new PrintStream(new FileOutputStream(path));
        walk(get(get(pl, "index"), "root"));
        out.close();
    }
    public static void dumpSource(Object source, String path) throws Exception {
        Object pl = findList(source, 0, Collections.newSetFromMap(new IdentityHashMap<>()));
        if (pl == null) throw new RuntimeException("no parameter list found");
        dumpList(pl, path);
    }
}
