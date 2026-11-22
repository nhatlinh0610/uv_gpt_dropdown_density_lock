// C++20 multi-threaded irregular 2D UV nesting solver for uv GPT.
// Self-contained: uses only C++20 stdlib + <thread>, zero third-party deps.

#include <algorithm>
#include <array>
#include <atomic>
#include <bit>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <limits>
#include <mutex>
#include <numeric>
#include <random>
#include <thread>
#include <vector>

namespace {

constexpr double kEps = 1.0e-9;
constexpr double kInf = 1.0e30;
constexpr int kMaxWords = 8; // Supports up to N = 512 bits per row in 1 cache line (64B).

struct Pt {
    double x;
    double y;
};

struct EdgeSeg {
    double ax, ay, bx, by;
    double minx, maxx, miny, maxy;
};

struct BBox {
    double minx, maxx, miny, maxy;
};

using Poly = std::vector<Pt>;
using ShapeLoops = std::vector<Poly>;
using ShapeEdges = std::vector<std::vector<EdgeSeg>>;

inline BBox bounds_of_pts(const std::vector<Pt>& pts) {
    if (pts.empty()) return {0.0, 0.0, 0.0, 0.0};
    double x0 = kInf, x1 = -kInf, y0 = kInf, y1 = -kInf;
    for (const auto& p : pts) {
        if (p.x < x0) x0 = p.x;
        if (p.x > x1) x1 = p.x;
        if (p.y < y0) y0 = p.y;
        if (p.y > y1) y1 = p.y;
    }
    return {x0, x1, y0, y1};
}

inline BBox bounds_of_shape(const ShapeLoops& sh) {
    double x0 = kInf, x1 = -kInf, y0 = kInf, y1 = -kInf;
    bool any = false;
    for (const auto& poly : sh) {
        for (const auto& p : poly) {
            any = true;
            if (p.x < x0) x0 = p.x;
            if (p.x > x1) x1 = p.x;
            if (p.y < y0) y0 = p.y;
            if (p.y > y1) y1 = p.y;
        }
    }
    return any ? BBox{x0, x1, y0, y1} : BBox{0.0, 0.0, 0.0, 0.0};
}

inline double signed_area(const Poly& poly) {
    const size_t n = poly.size();
    if (n < 3) return 0.0;
    double a = 0.0;
    for (size_t i = 0; i < n; ++i) {
        const Pt& p0 = poly[i];
        const Pt& p1 = poly[(i + 1) % n];
        a += p0.x * p1.y - p1.x * p0.y;
    }
    return 0.5 * a;
}

inline ShapeEdges build_shape_edges(const ShapeLoops& sh) {
    ShapeEdges out;
    out.reserve(sh.size());
    for (const auto& poly : sh) {
        std::vector<EdgeSeg> es;
        const size_t n = poly.size();
        es.reserve(n);
        for (size_t i = 0; i < n; ++i) {
            const Pt& a = poly[i];
            const Pt& b = poly[(i + 1) % n];
            es.push_back({
                a.x, a.y, b.x, b.y,
                std::min(a.x, b.x), std::max(a.x, b.x),
                std::min(a.y, b.y), std::max(a.y, b.y)
            });
        }
        out.push_back(std::move(es));
    }
    return out;
}

inline double orient(const Pt& a, const Pt& b, const Pt& c) {
    return (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x);
}

inline bool on_segment(const Pt& a, const Pt& b, const Pt& p) {
    return p.x >= std::min(a.x, b.x) - kEps && p.x <= std::max(a.x, b.x) + kEps &&
           p.y >= std::min(a.y, b.y) - kEps && p.y <= std::max(a.y, b.y) + kEps &&
           std::abs(orient(a, b, p)) <= kEps;
}

inline bool segments_intersect(const Pt& a, const Pt& b, const Pt& c, const Pt& d) {
    const double o1 = orient(a, b, c);
    const double o2 = orient(a, b, d);
    const double o3 = orient(c, d, a);
    const double o4 = orient(c, d, b);
    if (((o1 > kEps && o2 < -kEps) || (o1 < -kEps && o2 > kEps)) &&
        ((o3 > kEps && o4 < -kEps) || (o3 < -kEps && o4 > kEps))) {
        return true;
    }
    return on_segment(a, b, c) || on_segment(a, b, d) ||
           on_segment(c, d, a) || on_segment(c, d, b);
}

inline double point_segment_dist_sq(const Pt& p, const Pt& a, const Pt& b) {
    const double dx = b.x - a.x;
    const double dy = b.y - a.y;
    const double denom = dx * dx + dy * dy;
    if (denom <= kEps) {
        const double ux = p.x - a.x;
        const double uy = p.y - a.y;
        return ux * ux + uy * uy;
    }
    double t = ((p.x - a.x) * dx + (p.y - a.y) * dy) / denom;
    if (t < 0.0) t = 0.0;
    else if (t > 1.0) t = 1.0;
    const double qx = a.x + t * dx;
    const double qy = a.y + t * dy;
    const double ux = p.x - qx;
    const double uy = p.y - qy;
    return ux * ux + uy * uy;
}

inline bool point_in_polygon(const Pt& pt, const Poly& poly) {
    const size_t n = poly.size();
    if (n < 3) return false;
    bool inside = false;
    double xj = poly[n - 1].x;
    double yj = poly[n - 1].y;
    for (size_t i = 0; i < n; ++i) {
        const double xi = poly[i].x;
        const double yi = poly[i].y;
        if (pt.x >= std::min(xi, xj) - kEps && pt.x <= std::max(xi, xj) + kEps &&
            pt.y >= std::min(yi, yj) - kEps && pt.y <= std::max(yi, yj) + kEps &&
            std::abs((xi - xj) * (pt.y - yj) - (yi - yj) * (pt.x - xj)) <= kEps) {
            return true;
        }
        if ((yi > pt.y) != (yj > pt.y)) {
            if (pt.x < (xj - xi) * (pt.y - yi) / (yj - yi) + xi) {
                inside = !inside;
            }
        }
        xj = xi;
        yj = yi;
    }
    return inside;
}

inline bool point_in_shape(const Pt& pt, const ShapeLoops& sh) {
    bool inside = false;
    for (const auto& poly : sh) {
        if (point_in_polygon(pt, poly)) {
            inside = !inside;
        }
    }
    return inside;
}

inline bool shapes_conflict_exact(
    const ShapeLoops& left, const BBox& lb, const ShapeEdges& le_polys,
    const ShapeLoops& right, const BBox& rb, const ShapeEdges& re_polys,
    double margin, double m_sq
) {
    if (left.empty() || right.empty()) return false;
    if (lb.maxx + margin < rb.minx || rb.maxx + margin < lb.minx ||
        lb.maxy + margin < rb.miny || rb.maxy + margin < lb.miny) {
        return false;
    }
    for (const auto& le : le_polys) {
        for (const auto& re : re_polys) {
            for (const auto& e1 : le) {
                for (const auto& e2 : re) {
                    if (e1.maxx + margin < e2.minx || e2.maxx + margin < e1.minx ||
                        e1.maxy + margin < e2.miny || e2.maxy + margin < e1.miny) {
                        continue;
                    }
                    const Pt a{e1.ax, e1.ay}, b{e1.bx, e1.by}, c{e2.ax, e2.ay}, d{e2.bx, e2.by};
                    if (segments_intersect(a, b, c, d)) return true;
                    if (margin > 0.0) {
                        if (point_segment_dist_sq(a, c, d) < m_sq ||
                            point_segment_dist_sq(b, c, d) < m_sq ||
                            point_segment_dist_sq(c, a, b) < m_sq ||
                            point_segment_dist_sq(d, a, b) < m_sq) {
                            return true;
                        }
                    }
                }
            }
        }
    }
    for (const auto& poly : left) {
        if (!poly.empty()) {
            if (point_in_shape(poly[0], right)) return true;
            if (poly.size() > 2 && point_in_shape(poly[poly.size() / 2], right)) return true;
        }
    }
    for (const auto& poly : right) {
        if (!poly.empty()) {
            if (point_in_shape(poly[0], left)) return true;
            if (poly.size() > 2 && point_in_shape(poly[poly.size() / 2], left)) return true;
        }
    }
    return false;
}

class SpatialHash {
public:
    static constexpr int kRes = 36;
    struct Entry {
        ShapeLoops shape;
        BBox bounds;
        ShapeEdges edges;
        int owner_id = -1;
    };
    std::vector<Entry> entries;
    std::array<std::vector<int>, kRes * kRes> buckets;
    std::vector<int> stamp;
    int stamp_id = 0;

    SpatialHash() : stamp(0) {}

    void clear() {
        entries.clear();
        for (auto& b : buckets) b.clear();
        stamp.clear();
        stamp_id = 0;
    }

    void insert(ShapeLoops sh, BBox b, ShapeEdges e, int owner_id = -1) {
        const int idx = static_cast<int>(entries.size());
        entries.push_back({std::move(sh), b, std::move(e), owner_id});
        stamp.push_back(0);
        const int x0 = std::clamp(static_cast<int>(std::floor(b.minx * kRes)), 0, kRes - 1);
        const int x1 = std::clamp(static_cast<int>(std::floor(b.maxx * kRes)), 0, kRes - 1);
        const int y0 = std::clamp(static_cast<int>(std::floor(b.miny * kRes)), 0, kRes - 1);
        const int y1 = std::clamp(static_cast<int>(std::floor(b.maxy * kRes)), 0, kRes - 1);
        for (int gx = x0; gx <= x1; ++gx) {
            for (int gy = y0; gy <= y1; ++gy) {
                buckets[gx * kRes + gy].push_back(idx);
            }
        }
    }

    void rebuild_buckets() {
        for (auto& b : buckets) b.clear();
        for (int idx = 0; idx < static_cast<int>(entries.size()); ++idx) {
            const BBox& b = entries[idx].bounds;
            const int x0 = std::clamp(static_cast<int>(std::floor(b.minx * kRes)), 0, kRes - 1);
            const int x1 = std::clamp(static_cast<int>(std::floor(b.maxx * kRes)), 0, kRes - 1);
            const int y0 = std::clamp(static_cast<int>(std::floor(b.miny * kRes)), 0, kRes - 1);
            const int y1 = std::clamp(static_cast<int>(std::floor(b.maxy * kRes)), 0, kRes - 1);
            for (int gx = x0; gx <= x1; ++gx) {
                for (int gy = y0; gy <= y1; ++gy) {
                    buckets[gx * kRes + gy].push_back(idx);
                }
            }
        }
    }

    template <typename Fn>
    bool any_conflict(const BBox& b, double margin, Fn&& cb) {
        const int x0 = std::clamp(static_cast<int>(std::floor((b.minx - margin) * kRes)), 0, kRes - 1);
        const int x1 = std::clamp(static_cast<int>(std::floor((b.maxx + margin) * kRes)), 0, kRes - 1);
        const int y0 = std::clamp(static_cast<int>(std::floor((b.miny - margin) * kRes)), 0, kRes - 1);
        const int y1 = std::clamp(static_cast<int>(std::floor((b.maxy + margin) * kRes)), 0, kRes - 1);
        ++stamp_id;
        for (int gx = x0; gx <= x1; ++gx) {
            for (int gy = y0; gy <= y1; ++gy) {
                for (int idx : buckets[gx * kRes + gy]) {
                    if (stamp[idx] == stamp_id) continue;
                    stamp[idx] = stamp_id;
                    if (cb(entries[idx])) return true;
                }
            }
        }
        return false;
    }
};

struct HoleRect {
    double x0, x1, y0, y1;
};

struct RawUnitShape {
    // Polygons relative to unit's pivot center
    std::vector<ShapeLoops> shapes;
    double area = 0.0;
    double hull_area = 0.0;
    double outer_area = 0.0;
    bool has_hole = false;
    std::vector<double> angles;
};

struct UnitVariant {
    int angle_idx = 0;
    double angle = 0.0;
    double width = 0.0;
    double height = 0.0;
    double shift_x = 0.0; // minx of rotated centered polygons
    double shift_y = 0.0; // miny of rotated centered polygons
    std::vector<ShapeLoops> member_shapes;
    std::vector<BBox> shape_bounds;
    std::vector<ShapeEdges> shape_edges;
    std::vector<HoleRect> inner_holes; // unscaled local holes relative to variant bbox (0,0)
};

using BitRow = std::array<uint64_t, kMaxWords>;

struct PieceRun {
    int dx;
    int len;
};

struct PieceRowRuns {
    int dr;
    std::vector<PieceRun> runs;
};

struct CachedPiece {
    int var_idx = 0;
    int pw = 0;
    int ph = 0;
    std::vector<BitRow> prows;
    std::vector<PieceRowRuns> row_runs;
    std::vector<HoleRect> scaled_holes;
};

// -------------------- Bitset helpers (zero UB) --------------------

inline void row_clear(BitRow& r, int words) {
    for (int w = 0; w < words; ++w) r[w] = 0ULL;
}

inline void row_set_range(BitRow& r, int c0, int c1) {
    if (c1 < c0) return;
    int w0 = c0 >> 6;
    int w1 = c1 >> 6;
    int b0 = c0 & 63;
    int b1 = c1 & 63;
    if (w0 == w1) {
        uint64_t m = (~0ULL << b0);
        if (b1 < 63) m &= ((1ULL << (b1 + 1)) - 1ULL);
        r[w0] |= m;
    } else {
        r[w0] |= (~0ULL << b0);
        for (int w = w0 + 1; w < w1; ++w) r[w] = ~0ULL;
        uint64_t m1 = (b1 < 63) ? ((1ULL << (b1 + 1)) - 1ULL) : ~0ULL;
        r[w1] |= m1;
    }
}

inline void row_xor_range(BitRow& r, int c0, int c1) {
    if (c1 < c0) return;
    int w0 = c0 >> 6;
    int w1 = c1 >> 6;
    int b0 = c0 & 63;
    int b1 = c1 & 63;
    if (w0 == w1) {
        uint64_t m = (~0ULL << b0);
        if (b1 < 63) m &= ((1ULL << (b1 + 1)) - 1ULL);
        r[w0] ^= m;
    } else {
        r[w0] ^= (~0ULL << b0);
        for (int w = w0 + 1; w < w1; ++w) r[w] ^= ~0ULL;
        uint64_t m1 = (b1 < 63) ? ((1ULL << (b1 + 1)) - 1ULL) : ~0ULL;
        r[w1] ^= m1;
    }
}

inline bool row_any(const BitRow& r, int words) {
    for (int w = 0; w < words; ++w) {
        if (r[w]) return true;
    }
    return false;
}

inline int row_popcount(const BitRow& r, int words) {
    int s = 0;
    for (int w = 0; w < words; ++w) s += std::popcount(r[w]);
    return s;
}

inline int row_lsb(const BitRow& r, int words) {
    for (int w = 0; w < words; ++w) {
        if (r[w]) return (w << 6) + std::countr_zero(r[w]);
    }
    return -1;
}

inline int row_msb(const BitRow& r, int words) {
    for (int w = words - 1; w >= 0; --w) {
        if (r[w]) return (w << 6) + (63 - std::countl_zero(r[w]));
    }
    return -1;
}

inline void row_or_shift_left(BitRow& dst, const BitRow& src, int shift, int words, uint64_t last_word_mask) {
    const int ws = shift >> 6;
    const int bs = shift & 63;
    if (bs == 0) {
        for (int w = ws; w < words; ++w) {
            dst[w] |= src[w - ws];
        }
    } else {
        const int rbs = 64 - bs;
        for (int w = ws; w < words; ++w) {
            uint64_t v = src[w - ws] << bs;
            if (w - ws - 1 >= 0) v |= (src[w - ws - 1] >> rbs);
            dst[w] |= v;
        }
    }
    dst[words - 1] &= last_word_mask;
}

inline void row_dilate_1_h(BitRow& out, const BitRow& src, int words, uint64_t last_word_mask) {
    for (int w = 0; w < words; ++w) {
        uint64_t v = src[w];
        uint64_t sh_l = (v << 1) | ((w > 0) ? (src[w - 1] >> 63) : 0ULL);
        uint64_t sh_r = (v >> 1) | ((w + 1 < words) ? (src[w + 1] << 63) : 0ULL);
        out[w] = v | sh_l | sh_r;
    }
    out[words - 1] &= last_word_mask;
}

inline void row_shift_right(BitRow& dst, const BitRow& src, int shift, int words) {
    if (shift == 0) {
        for (int w = 0; w < words; ++w) dst[w] = src[w];
        return;
    }
    const int ws = shift >> 6;
    const int bs = shift & 63;
    if (bs == 0) {
        for (int w = 0; w < words; ++w) {
            dst[w] = (w + ws < words) ? src[w + ws] : 0ULL;
        }
    } else {
        const int lbs = 64 - bs;
        for (int w = 0; w < words; ++w) {
            uint64_t lo = (w + ws < words) ? (src[w + ws] >> bs) : 0ULL;
            uint64_t hi = (w + ws + 1 < words) ? (src[w + ws + 1] << lbs) : 0ULL;
            dst[w] = lo | hi;
        }
    }
}

inline void smear_left(const BitRow& src, BitRow& dst, int width, int words) {
    dst = src;
    if (width <= 1 || !row_any(src, words)) return;
    int rem = width - 1;
    int shift = 1;
    BitRow tmp{};
    while (rem > 0) {
        int step = (shift < rem) ? shift : rem;
        row_shift_right(tmp, dst, step, words);
        for (int w = 0; w < words; ++w) dst[w] |= tmp[w];
        rem -= step;
        shift <<= 1;
    }
}

inline std::vector<BitRow> rasterize_shapes_center(
    const std::vector<ShapeLoops>& shapes, double scale, double ox, double oy,
    int n, int num_rows, int num_cols
) {
    const int words = (num_cols + 63) >> 6;
    std::vector<BitRow> rows(num_rows);
    for (auto& r : rows) row_clear(r, words);
    const double inv_n = 1.0 / n;
    std::vector<double> xs;
    std::vector<Pt> pts;
    BitRow sh_row{};

    for (const auto& sh : shapes) {
        std::vector<BitRow> sh_rows(num_rows);
        for (auto& r : sh_rows) row_clear(r, words);
        bool any_scan = false;

        for (const auto& poly : sh) {
            if (poly.size() < 3) continue;
            pts.resize(poly.size());
            double xmin = kInf, xmax = -kInf, ymin = kInf, ymax = -kInf;
            for (size_t i = 0; i < poly.size(); ++i) {
                double px = ox + poly[i].x * scale;
                double py = oy + poly[i].y * scale;
                pts[i] = {px, py};
                if (px < xmin) xmin = px;
                if (px > xmax) xmax = px;
                if (py < ymin) ymin = py;
                if (py > ymax) ymax = py;
            }
            int r0 = std::max(0, static_cast<int>(std::floor(ymin * n)));
            int r1 = std::min(num_rows - 1, static_cast<int>(std::floor(ymax * n)));
            const size_t np = pts.size();
            for (int r = r0; r <= r1; ++r) {
                double y = (r + 0.5) * inv_n;
                xs.clear();
                for (size_t i = 0; i < np; ++i) {
                    const Pt& a = pts[i];
                    const Pt& b = pts[(i + 1) % np];
                    if ((a.y <= y && y < b.y) || (b.y <= y && y < a.y)) {
                        xs.push_back(a.x + (y - a.y) * (b.x - a.x) / (b.y - a.y));
                    }
                }
                if (xs.size() >= 2) {
                    std::sort(xs.begin(), xs.end());
                    for (size_t i = 0; i + 1 < xs.size(); i += 2) {
                        int c0 = std::max(0, static_cast<int>(std::ceil(xs[i] * n - 0.5)));
                        int c1 = std::min(num_cols - 1, static_cast<int>(std::floor(xs[i + 1] * n - 0.5)));
                        if (c1 >= c0) {
                            row_xor_range(sh_rows[r], c0, c1);
                            any_scan = true;
                        }
                    }
                }
            }
            if (std::min(xmax - xmin, ymax - ymin) * n < 1.5 || !any_scan) {
                const double max_x_t = num_cols * inv_n;
                const double max_y_t = num_rows * inv_n;
                for (size_t i = 0; i < np; ++i) {
                    const Pt& a = pts[i];
                    const Pt& b = pts[(i + 1) % np];
                    if (std::max(a.x, b.x) < 0.0 || std::min(a.x, b.x) > max_x_t ||
                        std::max(a.y, b.y) < 0.0 || std::min(a.y, b.y) > max_y_t) {
                        continue;
                    }
                    int steps = std::max(1, std::min(n * 2, static_cast<int>(std::max(std::abs(b.x - a.x), std::abs(b.y - a.y)) * n * 1.5) + 1));
                    for (int k = 0; k <= steps; ++k) {
                        double t = static_cast<double>(k) / steps;
                        int cr = static_cast<int>((a.y + (b.y - a.y) * t) * n);
                        int cc = static_cast<int>((a.x + (b.x - a.x) * t) * n);
                        if (cr >= 0 && cr < num_rows && cc >= 0 && cc < num_cols) {
                            sh_rows[cr][cc >> 6] |= (1ULL << (cc & 63));
                        }
                    }
                }
            }
        }
        for (int r = 0; r < num_rows; ++r) {
            for (int w = 0; w < words; ++w) rows[r][w] |= sh_rows[r][w];
        }
    }
    return rows;
}

inline std::vector<PieceRun> extract_runs(const BitRow& m, int words) {
    std::vector<PieceRun> runs;
    int col = 0;
    const int max_cols = words << 6;
    while (col < max_cols) {
        int w = col >> 6;
        uint64_t word = m[w] >> (col & 63);
        while (word == 0ULL) {
            col = (w + 1) << 6;
            if (col >= max_cols) return runs;
            w = col >> 6;
            word = m[w];
        }
        int tz = std::countr_zero(word);
        col += tz;
        int start = col;
        while (col < max_cols) {
            int cw = col >> 6;
            uint64_t cw_val = m[cw] >> (col & 63);
            uint64_t inv = ~cw_val;
            if (inv == 0ULL) {
                col = (cw + 1) << 6;
            } else {
                col += std::countr_zero(inv);
                break;
            }
        }
        runs.push_back({start, col - start});
    }
    return runs;
}

inline std::vector<HoleRect> extract_shape_holes(const std::vector<ShapeLoops>& shapes) {
    struct RankedHole {
        double area;
        HoleRect rect;
    };
    std::vector<RankedHole> ranked;
    for (const auto& sh : shapes) {
        if (sh.size() <= 1 || sh[0].size() < 3) continue;
        const Poly& outer = sh[0];
        for (size_t k = 1; k < sh.size(); ++k) {
            const Poly& poly = sh[k];
            if (poly.size() < 3) continue;
            double a = std::abs(signed_area(poly));
            if (a > 1.0e-4 && point_in_polygon(poly[0], outer)) {
                BBox b = bounds_of_pts(poly);
                ranked.push_back({a, {b.minx, b.maxx, b.miny, b.maxy}});
            }
        }
    }
    std::sort(ranked.begin(), ranked.end(), [](const auto& l, const auto& r) { return l.area > r.area; });
    std::vector<HoleRect> out;
    out.reserve(ranked.size());
    for (const auto& r : ranked) out.push_back(r.rect);
    return out;
}

inline std::vector<HoleRect> extract_bay_holes(
    const std::vector<BitRow>& prows, int pw, int ph, int n, int piece_words
) {
    if (pw < 16 || ph < 16) return {};
    int total_bits = 0;
    for (const auto& m : prows) total_bits += row_popcount(m, piece_words);
    if (total_bits >= 0.78 * pw * ph) return {};

    struct Span { int c0, c1, r0, r1; };
    struct Bay { double area; HoleRect rect; };
    const double inv_n = 1.0 / n;
    std::vector<Bay> bays;

    // 1) Row-wise scan (captures top/bottom vertical U/V bays)
    {
        std::vector<Span> closed_spans;
        bool have_cur = false;
        Span cur{};
        const int min_span_h = std::max(5, pw / 5);
        for (int r = 1; r < ph - 1; ++r) {
            int c_l = row_lsb(prows[r], piece_words);
            if (c_l < 0) {
                if (have_cur) { closed_spans.push_back(cur); have_cur = false; }
                continue;
            }
            int c_r = row_msb(prows[r], piece_words);
            if (c_r - c_l + 1 < min_span_h) {
                if (have_cur) { closed_spans.push_back(cur); have_cur = false; }
                continue;
            }
            BitRow inner{};
            row_clear(inner, piece_words);
            row_set_range(inner, c_l, c_r);
            for (int w = 0; w < piece_words; ++w) inner[w] &= ~prows[r][w];
            auto zero_runs = extract_runs(inner, piece_words);
            int best_dx = -1, best_len = 0;
            for (const auto& zr : zero_runs) {
                if (zr.len >= min_span_h && zr.len > best_len) {
                    best_dx = zr.dx;
                    best_len = zr.len;
                }
            }
            if (best_dx >= 0) {
                int sc0 = best_dx, sc1 = best_dx + best_len;
                if (have_cur && sc0 <= cur.c1 && sc1 >= cur.c0) {
                    cur.c0 = std::min(cur.c0, sc0);
                    cur.c1 = std::max(cur.c1, sc1);
                    cur.r1 = r + 1;
                } else {
                    if (have_cur) closed_spans.push_back(cur);
                    cur = {sc0, sc1, r, r + 1};
                    have_cur = true;
                }
            } else if (have_cur) {
                closed_spans.push_back(cur);
                have_cur = false;
            }
        }
        if (have_cur) closed_spans.push_back(cur);
        for (const auto& s : closed_spans) {
            if (s.c1 - s.c0 >= min_span_h && s.r1 - s.r0 >= 5) {
                double area = ((s.c1 - s.c0) * inv_n) * ((s.r1 - s.r0) * inv_n);
                if (area >= 0.0010) {
                    bays.push_back({area, {s.c0 * inv_n, s.c1 * inv_n, s.r0 * inv_n, s.r1 * inv_n}});
                }
            }
        }
    }

    // 2) Column-wise scan (captures left/right horizontal U/V bays on X-frames & rotated cutouts)
    {
        std::vector<Span> closed_cols;
        bool have_cur = false;
        Span cur{};
        const int min_span_v = std::max(5, ph / 5);
        for (int c = 1; c < pw - 1; ++c) {
            const int cw = c >> 6;
            const uint64_t bit = 1ULL << (c & 63);
            int r_bot = -1, r_top = -1;
            for (int r = 0; r < ph; ++r) {
                if (prows[r][cw] & bit) {
                    if (r_bot < 0) r_bot = r;
                    r_top = r;
                }
            }
            if (r_bot < 0 || r_top - r_bot + 1 < min_span_v) {
                if (have_cur) { closed_cols.push_back(cur); have_cur = false; }
                continue;
            }
            int best_r0 = -1, best_len = 0, run_start = -1;
            for (int r = r_bot + 1; r <= r_top; ++r) {
                bool filled = (prows[r][cw] & bit) != 0ULL;
                if (!filled) {
                    if (run_start < 0) run_start = r;
                } else if (run_start >= 0) {
                    int rlen = r - run_start;
                    if (rlen >= min_span_v && rlen > best_len) {
                        best_r0 = run_start;
                        best_len = rlen;
                    }
                    run_start = -1;
                }
            }
            if (best_r0 >= 0) {
                int sr0 = best_r0, sr1 = best_r0 + best_len;
                if (have_cur && sr0 <= cur.r1 && sr1 >= cur.r0) {
                    cur.c1 = c + 1;
                    cur.r0 = std::min(cur.r0, sr0);
                    cur.r1 = std::max(cur.r1, sr1);
                } else {
                    if (have_cur) closed_cols.push_back(cur);
                    cur = {c, c + 1, sr0, sr1};
                    have_cur = true;
                }
            } else if (have_cur) {
                closed_cols.push_back(cur);
                have_cur = false;
            }
        }
        if (have_cur) closed_cols.push_back(cur);
        for (const auto& s : closed_cols) {
            if (s.c1 - s.c0 >= 5 && s.r1 - s.r0 >= min_span_v) {
                double area = ((s.c1 - s.c0) * inv_n) * ((s.r1 - s.r0) * inv_n);
                if (area >= 0.0010) {
                    bays.push_back({area, {s.c0 * inv_n, s.c1 * inv_n, s.r0 * inv_n, s.r1 * inv_n}});
                }
            }
        }
    }

    std::sort(bays.begin(), bays.end(), [](const auto& a, const auto& b) { return a.area > b.area; });
    std::vector<HoleRect> out;
    for (const auto& b : bays) {
        bool dup = false;
        for (const auto& ex : out) {
            double ix0 = std::max(b.rect.x0, ex.x0), ix1 = std::min(b.rect.x1, ex.x1);
            double iy0 = std::max(b.rect.y0, ex.y0), iy1 = std::min(b.rect.y1, ex.y1);
            if (ix1 > ix0 && iy1 > iy0) {
                double inter = (ix1 - ix0) * (iy1 - iy0);
                double u = b.area + (ex.x1 - ex.x0) * (ex.y1 - ex.y0) - inter;
                if (u > 1e-12 && inter / u > 0.55) { dup = true; break; }
            }
        }
        if (!dup) {
            out.push_back(b.rect);
            if (out.size() >= 4) break;
        }
    }
    return out;
}

inline bool variant_conflicts(
    const UnitVariant& var, double scale, double tx, double ty,
    SpatialHash& index, double margin, double m_sq, int skip_owner = -1
) {
    const double low = margin - kEps;
    const double high = 1.0 - margin + kEps;
    for (const auto& b : var.shape_bounds) {
        if (b.minx * scale + tx < low || b.maxx * scale + tx > high ||
            b.miny * scale + ty < low || b.maxy * scale + ty > high) {
            return true;
        }
    }
    if (index.entries.empty()) return false;
    const double inv_scale = (scale > 1.0e-18) ? (1.0 / scale) : 0.0;

    for (size_t si = 0; si < var.member_shapes.size(); ++si) {
        const auto& b_loc = var.shape_bounds[si];
        const BBox tb{b_loc.minx * scale + tx, b_loc.maxx * scale + tx,
                      b_loc.miny * scale + ty, b_loc.maxy * scale + ty};
        const auto& edges_loc = var.shape_edges[si];
        const auto& sh_loc = var.member_shapes[si];

        bool hit = index.any_conflict(tb, margin, [&](const SpatialHash::Entry& other) {
            if (skip_owner >= 0 && other.owner_id == skip_owner) return false;
            const BBox& ob = other.bounds;
            if (tb.maxx + margin < ob.minx || ob.maxx + margin < tb.minx ||
                tb.maxy + margin < ob.miny || ob.maxy + margin < tb.miny) {
                return false;
            }
            for (const auto& poly_e : edges_loc) {
                for (const auto& le : poly_e) {
                    const double lax0 = le.minx * scale + tx, lax1 = le.maxx * scale + tx;
                    const double lay0 = le.miny * scale + ty, lay1 = le.maxy * scale + ty;
                    if (lax1 + margin < ob.minx || ob.maxx + margin < lax0 ||
                        lay1 + margin < ob.miny || ob.maxy + margin < lay0) {
                        continue;
                    }
                    const Pt a{le.ax * scale + tx, le.ay * scale + ty};
                    const Pt b{le.bx * scale + tx, le.by * scale + ty};
                    for (const auto& re_poly : other.edges) {
                        for (const auto& re : re_poly) {
                            if (lax1 + margin < re.minx || re.maxx + margin < lax0 ||
                                lay1 + margin < re.miny || re.maxy + margin < lay0) {
                                continue;
                            }
                            const Pt c{re.ax, re.ay}, d{re.bx, re.by};
                            if (segments_intersect(a, b, c, d)) return true;
                            if (margin > 0.0) {
                                if (point_segment_dist_sq(a, c, d) < m_sq ||
                                    point_segment_dist_sq(b, c, d) < m_sq ||
                                    point_segment_dist_sq(c, a, b) < m_sq ||
                                    point_segment_dist_sq(d, a, b) < m_sq) {
                                    return true;
                                }
                            }
                        }
                    }
                }
            }
            for (const auto& poly : sh_loc) {
                if (!poly.empty()) {
                    const Pt p0{poly[0].x * scale + tx, poly[0].y * scale + ty};
                    if (point_in_shape(p0, other.shape)) return true;
                    if (poly.size() > 2) {
                        const Pt& pm_loc = poly[poly.size() / 2];
                        const Pt pm{pm_loc.x * scale + tx, pm_loc.y * scale + ty};
                        if (point_in_shape(pm, other.shape)) return true;
                    }
                }
            }
            if (inv_scale > 0.0) {
                for (const auto& poly : other.shape) {
                    if (!poly.empty()) {
                        const Pt q0{(poly[0].x - tx) * inv_scale, (poly[0].y - ty) * inv_scale};
                        if (point_in_shape(q0, sh_loc)) return true;
                        if (poly.size() > 2) {
                            const Pt& qm_w = poly[poly.size() / 2];
                            const Pt qm{(qm_w.x - tx) * inv_scale, (qm_w.y - ty) * inv_scale};
                            if (point_in_shape(qm, sh_loc)) return true;
                        }
                    }
                }
            }
            return false;
        });
        if (hit) return true;
    }
    return false;
}

enum class SweepMode : int {
    BL_BOT = 0,
    BL_TOP = 1,
    LB_LEFT = 2,
    BR_BOT = 3,
    TL_TOP = 4,
    LB_RIGHT = 5,
    ENV = 6,
    MINMAX = 7,
    CONTACT_BL = 8,
    RADIAL = 9,
    ENV_X = 10
};

inline void compact_dir(
    const UnitVariant& var, double scale, double& x, double& y,
    SpatialHash& index, double margin, double m_sq, SweepMode mode, int skip_owner = -1
) {
    const double low = margin;
    const double hx = std::max(low, 1.0 - low - var.width * scale);
    const double hy = std::max(low, 1.0 - low - var.height * scale);
    const double tx_goal = (mode == SweepMode::BR_BOT) ? hx : low;
    const double ty_goal = (mode == SweepMode::TL_TOP) ? hy : low;

    auto slide_x = [&](double cx, double cy) -> double {
        if (std::abs(cx - tx_goal) <= 2.0e-5) return cx;
        bool move_low = (tx_goal <= low + 1.0e-9);
        double probe = std::clamp(cx + (move_low ? -0.008 : 0.008), low, hx);
        if (variant_conflicts(var, scale, probe, cy, index, margin, m_sq, skip_owner)) {
            double lo = move_low ? probe : cx;
            double hi = move_low ? cx : probe;
            for (int i = 0; i < 4; ++i) {
                double mid = 0.5 * (lo + hi);
                if (variant_conflicts(var, scale, mid, cy, index, margin, m_sq, skip_owner)) {
                    if (move_low) lo = mid; else hi = mid;
                } else {
                    if (move_low) hi = mid; else lo = mid;
                }
            }
            return move_low ? hi : lo;
        }
        if (!variant_conflicts(var, scale, tx_goal, cy, index, margin, m_sq, skip_owner)) return tx_goal;
        double lo = move_low ? tx_goal : probe;
        double hi = move_low ? probe : tx_goal;
        for (int i = 0; i < 5; ++i) {
            double mid = 0.5 * (lo + hi);
            if (variant_conflicts(var, scale, mid, cy, index, margin, m_sq, skip_owner)) {
                if (move_low) lo = mid; else hi = mid;
            } else {
                if (move_low) hi = mid; else lo = mid;
            }
        }
        return move_low ? hi : lo;
    };

    auto slide_y = [&](double cx, double cy) -> double {
        if (std::abs(cy - ty_goal) <= 2.0e-5) return cy;
        bool move_low = (ty_goal <= low + 1.0e-9);
        double probe = std::clamp(cy + (move_low ? -0.008 : 0.008), low, hy);
        if (variant_conflicts(var, scale, cx, probe, index, margin, m_sq, skip_owner)) {
            double lo = move_low ? probe : cy;
            double hi = move_low ? cy : probe;
            for (int i = 0; i < 4; ++i) {
                double mid = 0.5 * (lo + hi);
                if (variant_conflicts(var, scale, cx, mid, index, margin, m_sq, skip_owner)) {
                    if (move_low) lo = mid; else hi = mid;
                } else {
                    if (move_low) hi = mid; else lo = mid;
                }
            }
            return move_low ? hi : lo;
        }
        if (!variant_conflicts(var, scale, cx, ty_goal, index, margin, m_sq, skip_owner)) return ty_goal;
        double lo = move_low ? ty_goal : probe;
        double hi = move_low ? probe : ty_goal;
        for (int i = 0; i < 5; ++i) {
            double mid = 0.5 * (lo + hi);
            if (variant_conflicts(var, scale, cx, mid, index, margin, m_sq, skip_owner)) {
                if (move_low) lo = mid; else hi = mid;
            } else {
                if (move_low) hi = mid; else lo = mid;
            }
        }
        return move_low ? hi : lo;
    };

    if (mode == SweepMode::LB_LEFT || mode == SweepMode::LB_RIGHT) {
        x = slide_x(x, y);
        y = slide_y(x, y);
        x = slide_x(x, y);
    } else {
        y = slide_y(x, y);
        x = slide_x(x, y);
        y = slide_y(x, y);
    }
    // Also try diagonal (-1,-1) step + V-notch valley descent to settle smoothly into diagonal wedge pockets
    if (tx_goal <= low + 1.0e-9 && ty_goal <= low + 1.0e-9) {
        for (double step : {0.003, 0.001, 0.0003}) {
            double qx = std::max(low, x - step);
            double qy = std::max(low, y - step);
            if ((qx < x || qy < y) && !variant_conflicts(var, scale, qx, qy, index, margin, m_sq, skip_owner)) {
                x = qx;
                y = qy;
            }
        }
        for (double step : {0.004, 0.0015}) {
            if (y > low + 0.005) {
                double vx = std::min(hx, x + 0.5 * step);
                double vy = std::max(low, y - step);
                if (!variant_conflicts(var, scale, vx, vy, index, margin, m_sq, skip_owner)) {
                    x = vx;
                    y = vy;
                    y = slide_y(x, y);
                    x = slide_x(x, y);
                }
            }
            if (x > low + 0.005) {
                double vx = std::max(low, x - step);
                double vy = std::min(hy, y + 0.5 * step);
                if (!variant_conflicts(var, scale, vx, vy, index, margin, m_sq, skip_owner)) {
                    x = vx;
                    y = vy;
                    x = slide_x(x, y);
                    y = slide_y(x, y);
                }
            }
        }
    }
}

inline void insert_shape_into_index(SpatialHash& index, const UnitVariant& var, double scale, double px, double py, int owner_id = -1) {
    for (const auto& sh_loc : var.member_shapes) {
        ShapeLoops tsh(sh_loc.size());
        for (size_t pi = 0; pi < sh_loc.size(); ++pi) {
            tsh[pi].resize(sh_loc[pi].size());
            for (size_t vi = 0; vi < sh_loc[pi].size(); ++vi) {
                tsh[pi][vi] = {sh_loc[pi][vi].x * scale + px, sh_loc[pi][vi].y * scale + py};
            }
        }
        BBox tb = bounds_of_shape(tsh);
        ShapeEdges te = build_shape_edges(tsh);
        index.insert(std::move(tsh), tb, std::move(te), owner_id);
    }
}

struct PlacedUnit {
    double px = 0.0;
    double py = 0.0;
    int var_idx = 0;
};

struct UnitPrep {
    std::vector<UnitVariant> variants;
    double area = 0.0;
    double hull_area = 0.0;
    double outer_area = 0.0;
    bool has_hole = false;
    double max_dim = 0.0;
    double min_dim = 0.0;
    double solidity = 1.0;
    double min_var_box = kInf;
};

struct ScaleCache {
    int n = 320;
    int words = 5;
    uint64_t last_word_mask = ~0ULL;
    // Per unit -> list of CachedPiece
    std::vector<std::vector<CachedPiece>> unit_pieces;
};

inline void insert_board_rows(
    const std::vector<BitRow>& prows, int ph, int c0, int r0, int n, int words,
    int pad_cells, uint64_t last_mask,
    std::vector<BitRow>& rows_raw, std::vector<BitRow>& rows_pad,
    std::vector<int>& rver_raw, std::vector<int>& rver_pad
) {
    BitRow s{}, h{}, tmp{};
    for (int dr = 0; dr < ph; ++dr) {
        int rr = r0 + dr;
        if (rr < 0 || rr >= n) continue;
        if (!row_any(prows[dr], words)) continue;
        row_clear(s, words);
        row_or_shift_left(s, prows[dr], c0, words, last_mask);
        bool ch = false;
        for (int w = 0; w < words; ++w) {
            uint64_t nv = rows_raw[rr][w] | s[w];
            if (nv != rows_raw[rr][w]) { rows_raw[rr][w] = nv; ch = true; }
        }
        if (ch) ++rver_raw[rr];
        if (pad_cells > 0) {
            row_dilate_1_h(h, s, words, last_mask);
            for (int k = 1; k < pad_cells; ++k) {
                tmp = h;
                row_dilate_1_h(h, tmp, words, last_mask);
            }
            for (int off = -pad_cells; off <= pad_cells; ++off) {
                int tr = rr + off;
                if (tr < 0 || tr >= n) continue;
                const BitRow& add = (pad_cells == 1 && (off == -1 || off == 1)) ? s : h;
                bool cp = false;
                for (int w = 0; w < words; ++w) {
                    uint64_t nv = rows_pad[tr][w] | add[w];
                    if (nv != rows_pad[tr][w]) { rows_pad[tr][w] = nv; cp = true; }
                }
                if (cp) ++rver_pad[tr];
            }
        }
    }
}

struct SmearCacheEntry {
    uint64_t tag = 0;
    BitRow mask{};
};

struct CandHit {
    double k0, k1, k2, k3;
    double tx, ty;
    int var_idx;
    const CachedPiece* cp;
    bool operator<(const CandHit& o) const {
        if (k0 != o.k0) return k0 < o.k0;
        if (k1 != o.k1) return k1 < o.k1;
        if (k2 != o.k2) return k2 < o.k2;
        return k3 < o.k3;
    }
};

struct WorkerWorkspace {
    static constexpr uint32_t kSmearSlots = 4096;
    static constexpr uint32_t kSmearMask = kSmearSlots - 1;
    std::vector<SmearCacheEntry> smear_table;
    SpatialHash index;
    std::vector<BitRow> rows_raw;
    std::vector<BitRow> rows_pad;
    std::vector<BitRow> fc_rows;
    std::vector<int> rver_raw;
    std::vector<int> rver_pad;
    std::vector<HoleRect> active_holes;
    std::vector<CandHit> vhits;
    std::vector<CandHit> bm_hits;
    int ver_base = 0;
    int alloc_n = 0;

    void init(int n) {
        if (alloc_n != n) {
            alloc_n = n;
            rows_raw.assign(n, BitRow{});
            rows_pad.assign(n, BitRow{});
            fc_rows.assign(n, BitRow{});
            rver_raw.assign(n, 1);
            rver_pad.assign(n, 1);
        }
        if (smear_table.size() != kSmearSlots) {
            smear_table.resize(kSmearSlots);
        }
        if (vhits.capacity() < 512) vhits.reserve(512);
        if (bm_hits.capacity() < 512) bm_hits.reserve(512);
    }

    void reset_attempt(int n, int words, int max_updates = 2048) {
        init(n);
        index.clear();
        active_holes.clear();
        const int step = std::max(4096, max_updates + 128);
        if (ver_base <= 0 || ver_base > 1500000000 - step) {
            ver_base = 1;
            for (auto& e : smear_table) e.tag = 0;
        } else {
            ver_base += step;
        }
        for (int r = 0; r < n; ++r) {
            row_clear(rows_raw[r], words);
            row_clear(rows_pad[r], words);
            rver_raw[r] = ver_base;
            rver_pad[r] = ver_base;
        }
    }

    const BitRow& get_smeared(int rr, int rlen, bool use_pad, const BitRow& brow, int rv, int words) {
        const uint64_t tag = (static_cast<uint64_t>(static_cast<uint32_t>(rv)) << 21) |
                             (static_cast<uint64_t>(rlen & 1023) << 11) |
                             (static_cast<uint64_t>(rr & 1023) << 1) |
                             (use_pad ? 1ULL : 0ULL);
        const uint32_t slot_idx =
            ((static_cast<uint32_t>(rr) * 2654435761u) ^
             (static_cast<uint32_t>(rlen) * 40503u) ^
             (use_pad ? 2048u : 0u)) & kSmearMask;
        SmearCacheEntry& slot = smear_table[slot_idx];
        if (slot.tag != tag) {
            smear_left(brow, slot.mask, rlen, words);
            slot.tag = tag;
        }
        return slot.mask;
    }
};

inline int compute_contact_hits(
    const CachedPiece& cp, int c0, int r0, int n, int words, int mc,
    const std::vector<BitRow>& rows
) {
    int hits = 0;
    int step = std::max(1, cp.ph / 8);
    BitRow s{};
    for (int dr = 0; dr < cp.ph; dr += step) {
        if (!row_any(cp.prows[dr], words)) continue;
        int rr = r0 + dr;
        if (rr < 0 || rr >= n) continue;
        row_clear(s, words);
        row_or_shift_left(s, cp.prows[dr], c0, words, ~0ULL);
        for (int w = 0; w < words; ++w) {
            uint64_t sw = s[w];
            uint64_t adj_h = (sw << 1) | (sw >> 1) |
                             ((w > 0) ? (s[w - 1] >> 63) : 0ULL) |
                             ((w + 1 < words) ? (s[w + 1] << 63) : 0ULL);
            hits += std::popcount(adj_h & rows[rr][w]);
            if (rr > 0) hits += std::popcount(sw & rows[rr - 1][w]);
            if (rr + 1 < n) hits += std::popcount(sw & rows[rr + 1][w]);
        }
    }
    if (c0 <= mc + 1 || c0 + cp.pw >= n - mc - 1) hits += cp.ph / step;
    if (r0 <= mc + 1 || r0 + cp.ph >= n - mc - 1) hits += cp.pw / 2;
    return hits;
}

inline int extract_corner_cols(
    const BitRow& fc, const BitRow& prev_fc, int words, int c_lo, int c_hi,
    bool all_corners, SweepMode mode, std::array<int, 32>& cols_buf, bool use_pad
) {
    int ncols = 0;
    if (all_corners) {
        for (int w = 0; w < words && ncols < 18; ++w) {
            uint64_t fw = fc[w];
            if (!fw) continue;
            uint64_t prev_bit = (w > 0) ? (fc[w - 1] >> 63) : 0ULL;
            uint64_t next_bit = (w + 1 < words) ? (fc[w + 1] << 63) : 0ULL;
            uint64_t lr = (fw & ~((fw << 1) | prev_bit)) | (fw & ~((fw >> 1) | next_bit));
            uint64_t tb = fw & ~prev_fc[w];
            uint64_t m = lr | (tb & ~((tb << 1) | prev_bit));
            while (m && ncols < 18) {
                int b = std::countr_zero(m);
                m &= (m - 1ULL);
                int c = (w << 6) + b;
                if (c >= c_lo && c <= c_hi) {
                    if (ncols > 0 && c - cols_buf[ncols - 1] <= 2) continue;
                    cols_buf[ncols++] = c;
                }
            }
        }
        if (ncols == 0) {
            int cl = row_lsb(fc, words);
            if (cl >= 0) cols_buf[ncols++] = cl;
        }
    } else {
        int cl = row_lsb(fc, words);
        int cr = row_msb(fc, words);
        if (mode == SweepMode::BR_BOT) {
            if (cr >= 0) cols_buf[ncols++] = cr;
        } else {
            if (cl >= 0) cols_buf[ncols++] = cl;
            if (!use_pad && cr >= 0 && cr != cl) cols_buf[ncols++] = cr;
        }
    }
    return ncols;
}

bool pack_unit_subset(
    const std::vector<int>& subset,
    WorkerWorkspace& ws,
    const std::vector<UnitPrep>& unit_preps,
    const ScaleCache& cache,
    double scale,
    double margin,
    double m_sq,
    SweepMode mode,
    int max_vars,
    bool raw_first,
    bool compare_all_variants,
    const std::atomic<bool>& stop_flag,
    std::chrono::steady_clock::time_point deadline,
    std::vector<PlacedUnit>& out_places,
    double& env_w,
    double& env_h,
    int& env_c,
    int& env_r,
    int& out_fail_pos,
    int& out_fail_uid
) {
    const int n = cache.n;
    const int words = cache.words;
    const uint64_t last_mask = cache.last_word_mask;
    const double inv_n = 1.0 / n;
    const int mc = std::max(0, static_cast<int>(std::floor(margin * n)));
    const int pad_cells = (margin > 1.0e-6) ? std::min(4, std::max(1, static_cast<int>(std::llround(margin * n * 0.92)))) : 0;
    const double high_lim = 1.0 - 2.0 * margin + kEps;
    const double nd = std::max(margin + 2.0e-4, 0.75 * inv_n);

    const std::array<Pt, 5> nudges_pad{{{0, 0}, {nd * 0.45, 0}, {0, nd * 0.45}, {-nd * 0.45, 0}, {0, -nd * 0.45}}};
    const std::array<Pt, 8> nudges_raw{{{0, 0}, {nd, 0}, {0, nd}, {-nd, 0}, {0, -nd}, {nd, nd}, {nd, -nd}, {-nd, nd}}};

    BitRow fc{}, vcm{}, tmp_sm{}, prev_fc{};
    const int total = static_cast<int>(subset.size());
    const bool cmp_mode_supported = (mode == SweepMode::ENV || mode == SweepMode::ENV_X ||
                                     mode == SweepMode::MINMAX || mode == SweepMode::CONTACT_BL);
    const bool use_cmp_all = compare_all_variants && cmp_mode_supported;

    for (int pos = 0; pos < total; ++pos) {
        if ((pos & 1) == 0) {
            if (stop_flag.load(std::memory_order_relaxed) || std::chrono::steady_clock::now() > deadline) {
                out_fail_pos = pos;
                out_fail_uid = -1;
                return false;
            }
        }
        const int uid = subset[pos];
        const auto& up = unit_preps[uid];
        const auto& pieces = cache.unit_pieces[uid];
        if (pieces.empty()) {
            out_fail_pos = pos;
            out_fail_uid = uid;
            return false;
        }
        const int total_pieces = static_cast<int>(pieces.size());
        int primary_cnt = 0;
        const double p_thresh = up.min_var_box * 1.18;
        for (const auto& cp_chk : pieces) {
            const auto& v_chk = up.variants[cp_chk.var_idx];
            if (v_chk.width * v_chk.height <= p_thresh) ++primary_cnt;
            else break;
        }
        primary_cnt = std::max(1, primary_cnt);
        const int eval_cap = use_cmp_all
            ? ((up.solidity < 0.78) ? std::min(total_pieces, std::min(max_vars, (pos < 24) ? 10 : 6))
                                    : std::min(total_pieces, (max_vars >= 16 && pos < 20) ? 8 : std::min(max_vars, std::max(4, primary_cnt))))
            : ((up.solidity < 0.78) ? std::min(total_pieces, (max_vars > 6 && pos >= 20) ? 6 : max_vars)
                                    : std::min(total_pieces, (max_vars >= 16 && pos < 20) ? 10 : std::max(4, std::min(6, primary_cnt + 2))));

        bool has_chosen = false;
        double chosen_x = 0.0, chosen_y = 0.0;
        const CachedPiece* chosen_cp = nullptr;

        // 1) Explicit interior hole / bay nesting check (expanded for medium pieces + 24 bays)
        if (!ws.active_holes.empty() && up.area < 0.045) {
            for (size_t hi = 0; hi < std::min<size_t>(24, ws.active_holes.size()); ++hi) {
                const HoleRect& hr = ws.active_holes[hi];
                const double hw = hr.x1 - hr.x0, hh = hr.y1 - hr.y0;
                const int hole_var_cap = std::min(8, eval_cap);
                for (int vi = 0; vi < hole_var_cap; ++vi) {
                    const auto& cp = pieces[vi];
                    const auto& var = up.variants[cp.var_idx];
                    const double vw = var.width * scale, vh = var.height * scale;
                    if (vw + 2.0 * margin > hw || vh + 2.0 * margin > hh) continue;
                    const int r_lo = std::max(mc, static_cast<int>(std::floor((hr.y0 + margin) * n)));
                    const int r_hi = std::min(n - mc - cp.ph, static_cast<int>(std::ceil((hr.y1 - margin - vh) * n)));
                    const int c_lo = std::max(mc, static_cast<int>(std::floor((hr.x0 + margin) * n)));
                    const int c_hi = std::min(n - mc - cp.pw, static_cast<int>(std::ceil((hr.x1 - margin - vw) * n)));
                    if (r_hi < r_lo || c_hi < c_lo) continue;
                    row_clear(vcm, words);
                    row_set_range(vcm, c_lo, c_hi);
                    const double hx = std::max(margin, 1.0 - margin - vw);
                    const double hy = std::max(margin, 1.0 - margin - vh);

                    for (int r0 = r_lo; r0 <= r_hi; ++r0) {
                        fc = vcm;
                        for (const auto& rr_item : cp.row_runs) {
                            const int rr = r0 + rr_item.dr;
                            const BitRow& brow = ws.rows_raw[rr];
                            if (!row_any(brow, words)) continue;
                            const int rv = ws.rver_raw[rr];
                            for (const auto& pr : rr_item.runs) {
                                const BitRow& sm = ws.get_smeared(rr, pr.len, false, brow, rv, words);
                                row_shift_right(tmp_sm, sm, pr.dx, words);
                                for (int w = 0; w < words; ++w) fc[w] &= ~tmp_sm[w];
                                if (!row_any(fc, words)) break;
                            }
                            if (!row_any(fc, words)) break;
                        }
                        if (!row_any(fc, words)) continue;
                        int c0 = row_lsb(fc, words);
                        double tx = std::min(std::max(margin, c0 * inv_n), hx);
                        double ty = std::min(std::max(margin, r0 * inv_n), hy);
                        for (const auto& nd_pt : nudges_raw) {
                            double qx = std::min(std::max(margin, tx + nd_pt.x), hx);
                            double qy = std::min(std::max(margin, ty + nd_pt.y), hy);
                            if (!variant_conflicts(var, scale, qx, qy, ws.index, margin, m_sq)) {
                                compact_dir(var, scale, qx, qy, ws.index, margin, m_sq, SweepMode::BL_BOT);
                                chosen_x = qx; chosen_y = qy; chosen_cp = &cp; has_chosen = true;
                                break;
                            }
                        }
                        if (has_chosen) break;
                    }
                    if (has_chosen) break;
                }
                if (has_chosen) break;
            }
        }

        // 2) Bitset sweep across variants
        if (!has_chosen) {
            const int start_pass = (pad_cells > 0 && !raw_first) ? 0 : (pad_cells > 0 ? 1 : 0);
            const int pass_cnt = (pad_cells > 0) ? 2 : 1;
            const bool all_corners = cmp_mode_supported;

            auto collect_var_hits = [&](const CachedPiece& cp, const UnitVariant& var, bool use_pad) {
                const double vw = var.width * scale, vh = var.height * scale;
                if (vw > high_lim || vh > high_lim) return;
                const double hx = std::max(margin, 1.0 - margin - vw);
                const double hy = std::max(margin, 1.0 - margin - vh);
                const int r_lo = mc, r_hi = n - mc - cp.ph;
                const int c_lo = mc, c_hi = n - mc - cp.pw;
                if (r_hi < r_lo || c_hi < c_lo) return;

                const double bloat_px = (pieces.size() > 4 && up.solidity >= 0.78)
                    ? (3.0 * ((var.width * var.height) / std::max(up.min_var_box, 1.0e-12) - 1.0))
                    : 0.0;

                const auto& rows = use_pad ? ws.rows_pad : ws.rows_raw;
                const auto& rver = use_pad ? ws.rver_pad : ws.rver_raw;
                row_clear(vcm, words);
                row_set_range(vcm, c_lo, c_hi);

                const bool rev_r = (mode == SweepMode::TL_TOP);
                const int r_start = rev_r ? r_hi : r_lo;
                const int r_end = rev_r ? (r_lo - 1) : (r_hi + 1);
                const int r_step = rev_r ? -1 : 1;
                row_clear(prev_fc, words);

                int first_hit_r0 = -1;
                std::array<int, 32> cols_buf{};

                auto emit_row_cols = [&](int r0, const int* cb, int ncols) {
                    for (int ci = 0; ci < ncols; ++ci) {
                        int c0 = cb[ci];
                        double tx = std::min(std::max(margin, c0 * inv_n), hx);
                        double ty = std::min(std::max(margin, r0 * inv_n), hy);
                        double k0 = 0, k1 = 0, k2 = 0, k3 = 0;
                        switch (mode) {
                            case SweepMode::BL_BOT:
                                k0 = r0 + bloat_px; k1 = c0; break;
                            case SweepMode::BR_BOT:
                                k0 = r0 + bloat_px; k1 = n - c0 - cp.pw; break;
                            case SweepMode::TL_TOP:
                                k0 = -(r0 + cp.ph) + bloat_px; k1 = c0; break;
                            case SweepMode::BL_TOP:
                                k0 = r0 + cp.ph + bloat_px; k1 = c0 + cp.pw; k2 = r0; k3 = c0; break;
                            case SweepMode::LB_LEFT:
                                k0 = c0 + bloat_px; k1 = r0; break;
                            case SweepMode::LB_RIGHT:
                                k0 = c0 + cp.pw + bloat_px; k1 = r0 + cp.ph; k2 = c0; k3 = r0; break;
                            case SweepMode::ENV: {
                                int nw = std::max(env_c, c0 + cp.pw);
                                int nh = std::max(env_r, r0 + cp.ph);
                                k0 = static_cast<double>(nw) * nh + bloat_px * n;
                                k1 = std::max(nw, nh);
                                k2 = r0 + cp.ph;
                                k3 = c0 + cp.pw;
                                break;
                            }
                            case SweepMode::ENV_X: {
                                int nw = std::max(env_c, c0 + cp.pw);
                                int nh = std::max(env_r, r0 + cp.ph);
                                k0 = static_cast<double>(nw) * nh + bloat_px * n;
                                k1 = std::max(nw, nh);
                                k2 = c0 + cp.pw;
                                k3 = r0 + cp.ph;
                                break;
                            }
                            case SweepMode::MINMAX: {
                                int nw = std::max(env_c, c0 + cp.pw);
                                int nh = std::max(env_r, r0 + cp.ph);
                                k0 = std::max(nw, nh) + bloat_px;
                                k1 = nw + nh;
                                k2 = r0 + cp.ph;
                                k3 = c0 + cp.pw;
                                break;
                            }
                            case SweepMode::CONTACT_BL: {
                                int hits = compute_contact_hits(cp, c0, r0, n, words, mc, ws.rows_raw);
                                int nw = std::max(env_c, c0 + cp.pw);
                                int nh = std::max(env_r, r0 + cp.ph);
                                k0 = static_cast<double>(nw) * nh + bloat_px * n - 16.0 * hits;
                                k1 = -hits;
                                k2 = r0 + cp.ph;
                                k3 = c0 + cp.pw;
                                break;
                            }
                            case SweepMode::RADIAL: {
                                double cxp = c0 + 0.5 * cp.pw;
                                double cyp = r0 + 0.5 * cp.ph;
                                k0 = cxp * cxp + cyp * cyp + bloat_px * n;
                                k1 = r0; k2 = c0;
                                break;
                            }
                        }
                        ws.vhits.push_back({k0, k1, k2, k3, tx, ty, cp.var_idx, &cp});
                    }
                };

                for (int r0 = r_start; r0 != r_end; r0 += r_step) {
                    if (use_cmp_all && use_pad && static_cast<int>(ws.vhits.size()) >= 36) {
                        break;
                    }
                    if (!use_cmp_all && use_pad && static_cast<int>(ws.vhits.size()) >= 6) {
                        if (mode == SweepMode::BL_BOT || mode == SweepMode::BR_BOT || mode == SweepMode::TL_TOP) break;
                        if (mode == SweepMode::BL_TOP && (r0 + cp.ph) > static_cast<int>(ws.vhits[0].k0) + 4) break;
                    } else if (!use_cmp_all && !use_pad &&
                               (mode == SweepMode::BL_BOT || mode == SweepMode::BR_BOT || mode == SweepMode::TL_TOP) &&
                               static_cast<int>(ws.vhits.size()) >= 16 && first_hit_r0 >= 0 && std::abs(r0 - first_hit_r0) > 12) {
                        break;
                    }

                    fc = vcm;
                    for (const auto& rr_item : cp.row_runs) {
                        const int rr = r0 + rr_item.dr;
                        const BitRow& brow = rows[rr];
                        if (!row_any(brow, words)) continue;
                        const int rv = rver[rr];
                        for (const auto& pr : rr_item.runs) {
                            const BitRow& sm = ws.get_smeared(rr, pr.len, use_pad, brow, rv, words);
                            row_shift_right(tmp_sm, sm, pr.dx, words);
                            for (int w = 0; w < words; ++w) fc[w] &= ~tmp_sm[w];
                            if (!row_any(fc, words)) break;
                        }
                        if (!row_any(fc, words)) break;
                    }
                    if (!row_any(fc, words)) {
                        row_clear(prev_fc, words);
                        continue;
                    }

                    const int ncols = extract_corner_cols(fc, prev_fc, words, c_lo, c_hi, all_corners, mode, cols_buf, use_pad);
                    if (ncols > 0) {
                        if (first_hit_r0 < 0) first_hit_r0 = r0;
                        emit_row_cols(r0, cols_buf.data(), ncols);
                    }
                    prev_fc = fc;
                }
            };

            for (int pass = start_pass; pass < pass_cnt && !has_chosen; ++pass) {
                const bool use_pad = (pad_cells > 0) && (pass == 0);
                const Pt* n_ptr = use_pad ? nudges_pad.data() : nudges_raw.data();
                const int n_cnt = use_pad ? static_cast<int>(nudges_pad.size()) : static_cast<int>(nudges_raw.size());

                if (use_cmp_all) {
                    bool best_var_has = false;
                    double b_s0 = kInf, b_s1 = kInf, b_s2 = kInf, b_s3 = kInf;
                    double b_x = 0.0, b_y = 0.0;
                    const CachedPiece* b_cp = nullptr;
                    const int test_cap = use_pad ? 28 : 44;

                    for (int vi = 0; vi < eval_cap; ++vi) {
                        if ((vi & 3) == 3) {
                            if (stop_flag.load(std::memory_order_relaxed) || std::chrono::steady_clock::now() > deadline) {
                                out_fail_pos = pos;
                                out_fail_uid = -1;
                                return false;
                            }
                        }
                        const auto& cp = pieces[vi];
                        const auto& var = up.variants[cp.var_idx];
                        ws.vhits.clear();
                        collect_var_hits(cp, var, use_pad);
                        if (ws.vhits.empty()) continue;
                        std::sort(ws.vhits.begin(), ws.vhits.end());

                        const double vw = var.width * scale, vh = var.height * scale;
                        const double hx = std::max(margin, 1.0 - margin - vw);
                        const double hy = std::max(margin, 1.0 - margin - vh);
                        const int m_v = static_cast<int>(ws.vhits.size());
                        const int head_v = std::min(m_v, std::max(10, test_cap / 2));

                        auto eval_hit = [&](const CandHit& ch) -> bool {
                            for (int ni = 0; ni < n_cnt; ++ni) {
                                double qx = std::min(std::max(margin, ch.tx + n_ptr[ni].x), hx);
                                double qy = std::min(std::max(margin, ch.ty + n_ptr[ni].y), hy);
                                if (!variant_conflicts(var, scale, qx, qy, ws.index, margin, m_sq)) {
                                    compact_dir(var, scale, qx, qy, ws.index, margin, m_sq, mode);
                                    const double nw = std::max(env_w, qx + vw);
                                    const double nh = std::max(env_h, qy + vh);
                                    const double box_pen = (pieces.size() > 4 && up.solidity >= 0.78) ? (0.07 * vw * vh) : 0.0;
                                    double s0 = 0.0, s1 = 0.0, s2 = 0.0, s3 = 0.0;
                                    if (mode == SweepMode::ENV_X) {
                                        s0 = nw * nh + box_pen; s1 = std::max(nw, nh); s2 = qx + vw; s3 = qy + vh;
                                    } else if (mode == SweepMode::ENV) {
                                        s0 = nw * nh + box_pen; s1 = std::max(nw, nh); s2 = qy + vh; s3 = qx + vw;
                                    } else if (mode == SweepMode::MINMAX) {
                                        s0 = std::max(nw, nh) + box_pen; s1 = nw + nh; s2 = qy + vh; s3 = qx + vw;
                                    } else {
                                        int c0p = std::clamp(static_cast<int>(std::llround(qx * n)), 0, n - 1);
                                        int r0p = std::clamp(static_cast<int>(std::llround(qy * n)), 0, n - 1);
                                        int chits = compute_contact_hits(cp, c0p, r0p, n, words, mc, ws.rows_raw);
                                        s0 = nw * nh + box_pen - 0.00018 * chits;
                                        s1 = -static_cast<double>(chits);
                                        s2 = qy + vh;
                                        s3 = qx + vw;
                                    }
                                    if (!best_var_has || std::tie(s0, s1, s2, s3) < std::tie(b_s0, b_s1, b_s2, b_s3)) {
                                        best_var_has = true;
                                        b_s0 = s0; b_s1 = s1; b_s2 = s2; b_s3 = s3;
                                        b_x = qx; b_y = qy; b_cp = &cp;
                                    }
                                    return true;
                                }
                            }
                            return false;
                        };

                        bool got_v = false;
                        for (int i = 0; i < head_v; ++i) {
                            if (eval_hit(ws.vhits[i])) { got_v = true; break; }
                        }
                        if (!got_v && m_v > head_v) {
                            int tail_cnt = std::min(m_v - head_v, test_cap - head_v);
                            int rest = m_v - head_v;
                            for (int i = 0; i < tail_cnt; ++i) {
                                int idx_v = head_v + (i * rest) / tail_cnt;
                                if (eval_hit(ws.vhits[idx_v])) break;
                            }
                        }
                    }
                    if (best_var_has) {
                        chosen_x = b_x; chosen_y = b_y; chosen_cp = b_cp; has_chosen = true;
                    }
                } else {
                    ws.bm_hits.clear();
                    const int rows_per_var = use_pad ? 8 : 16;
                    for (int vi = 0; vi < eval_cap; ++vi) {
                        if ((vi & 3) == 3) {
                            if (stop_flag.load(std::memory_order_relaxed) || std::chrono::steady_clock::now() > deadline) {
                                out_fail_pos = pos;
                                out_fail_uid = -1;
                                return false;
                            }
                        }
                        const auto& cp = pieces[vi];
                        const auto& var = up.variants[cp.var_idx];
                        ws.vhits.clear();
                        collect_var_hits(cp, var, use_pad);
                        if (!ws.vhits.empty()) {
                            std::sort(ws.vhits.begin(), ws.vhits.end());
                            if (static_cast<int>(ws.vhits.size()) <= rows_per_var) {
                                ws.bm_hits.insert(ws.bm_hits.end(), ws.vhits.begin(), ws.vhits.end());
                            } else {
                                int head = std::max(1, std::min(rows_per_var, rows_per_var / 2));
                                ws.bm_hits.insert(ws.bm_hits.end(), ws.vhits.begin(), ws.vhits.begin() + head);
                                int tail = rows_per_var - head;
                                if (tail > 0) {
                                    int rest = static_cast<int>(ws.vhits.size()) - head;
                                    for (int i = 0; i < tail; ++i) {
                                        ws.bm_hits.push_back(ws.vhits[head + (i * rest) / tail]);
                                    }
                                }
                            }
                        }
                    }
                    if (ws.bm_hits.empty()) continue;
                    std::sort(ws.bm_hits.begin(), ws.bm_hits.end());
                    int hit_cap = use_pad ? 20 : 52;
                    int checked = 0;
                    const int m_hits = static_cast<int>(ws.bm_hits.size());
                    int head_chk = std::min(m_hits, std::max(6, hit_cap / 2));
                    auto try_cand = [&](const CandHit& ch) -> bool {
                        const auto& var = up.variants[ch.cp->var_idx];
                        const double vw = var.width * scale, vh = var.height * scale;
                        const double hx = std::max(margin, 1.0 - margin - vw);
                        const double hy = std::max(margin, 1.0 - margin - vh);
                        for (int ni = 0; ni < n_cnt; ++ni) {
                            double qx = std::min(std::max(margin, ch.tx + n_ptr[ni].x), hx);
                            double qy = std::min(std::max(margin, ch.ty + n_ptr[ni].y), hy);
                            if (!variant_conflicts(var, scale, qx, qy, ws.index, margin, m_sq)) {
                                compact_dir(var, scale, qx, qy, ws.index, margin, m_sq, mode);
                                chosen_x = qx; chosen_y = qy; chosen_cp = ch.cp; has_chosen = true;
                                return true;
                            }
                        }
                        return false;
                    };
                    for (int i = 0; i < head_chk; ++i) {
                        ++checked;
                        if (try_cand(ws.bm_hits[i])) break;
                    }
                    if (!has_chosen && m_hits > head_chk) {
                        int tail_cnt = std::min(m_hits - head_chk, hit_cap - checked);
                        if (tail_cnt > 0) {
                            int rest = m_hits - head_chk;
                            for (int i = 0; i < tail_cnt; ++i) {
                                int idx_h = head_chk + (i * rest) / tail_cnt;
                                if (try_cand(ws.bm_hits[idx_h])) break;
                            }
                        }
                    }
                }
            }
        }

        if (!has_chosen) {
            out_fail_pos = pos;
            out_fail_uid = uid;
            return false;
        }

        const auto& chosen_var = up.variants[chosen_cp->var_idx];
        insert_shape_into_index(ws.index, chosen_var, scale, chosen_x, chosen_y, uid);
        int c0 = std::clamp(static_cast<int>(std::llround(chosen_x * n)), 0, n - 1);
        int r0 = std::clamp(static_cast<int>(std::llround(chosen_y * n)), 0, n - 1);
        insert_board_rows(chosen_cp->prows, chosen_cp->ph, c0, r0, n, words, pad_cells, last_mask,
                          ws.rows_raw, ws.rows_pad, ws.rver_raw, ws.rver_pad);
        for (const auto& hr : chosen_cp->scaled_holes) {
            ws.active_holes.push_back({chosen_x + hr.x0, chosen_x + hr.x1, chosen_y + hr.y0, chosen_y + hr.y1});
        }
        if (ws.active_holes.size() > 24) {
            std::sort(ws.active_holes.begin(), ws.active_holes.end(), [](const HoleRect& a, const HoleRect& b) {
                return (a.x1 - a.x0) * (a.y1 - a.y0) > (b.x1 - b.x0) * (b.y1 - b.y0);
            });
            ws.active_holes.resize(24);
        }
        env_w = std::max(env_w, chosen_x + chosen_var.width * scale);
        env_h = std::max(env_h, chosen_y + chosen_var.height * scale);
        env_c = std::max(env_c, static_cast<int>(std::ceil((chosen_x + chosen_var.width * scale) * n)));
        env_r = std::max(env_r, static_cast<int>(std::ceil((chosen_y + chosen_var.height * scale) * n)));
        out_places[uid] = {chosen_x, chosen_y, chosen_var.angle_idx};
    }
    return true;
}

bool try_pack_once(
    const std::vector<int>& ordered,
    WorkerWorkspace& ws,
    const std::vector<UnitPrep>& unit_preps,
    const std::vector<ShapeLoops>& static_shapes,
    const ScaleCache& cache,
    double scale,
    double margin,
    double m_sq,
    SweepMode mode,
    int max_vars,
    bool raw_first,
    bool compare_all_variants,
    const std::atomic<bool>& stop_flag,
    std::chrono::steady_clock::time_point deadline,
    std::vector<PlacedUnit>& out_places,
    int& out_fail_pos,
    int& out_fail_uid
) {
    const int n = cache.n;
    const int words = cache.words;
    const uint64_t last_mask = cache.last_word_mask;
    const int pad_cells = (margin > 1.0e-6) ? std::min(4, std::max(1, static_cast<int>(std::llround(margin * n * 0.92)))) : 0;
    const int mc = std::max(0, static_cast<int>(std::floor(margin * n)));

    ws.reset_attempt(n, words, static_cast<int>(ordered.size() + static_shapes.size() + 32));
    double env_w = margin, env_h = margin;
    int env_c = mc, env_r = mc;

    for (const auto& sh : static_shapes) {
        BBox sb = bounds_of_shape(sh);
        ws.index.insert(sh, sb, build_shape_edges(sh), -1);
        if (sb.maxx > -margin - 0.01 && sb.minx < 1.0 + margin + 0.01 &&
            sb.maxy > -margin - 0.01 && sb.miny < 1.0 + margin + 0.01) {
            env_w = std::max(env_w, std::min(1.0 - margin, sb.maxx));
            env_h = std::max(env_h, std::min(1.0 - margin, sb.maxy));
            auto sr = rasterize_shapes_center({sh}, 1.0, 0.0, 0.0, n, n, n);
            insert_board_rows(sr, n, 0, 0, n, words, pad_cells, last_mask,
                              ws.rows_raw, ws.rows_pad, ws.rver_raw, ws.rver_pad);
            for (const auto& h : extract_shape_holes({sh})) {
                double cx0 = std::max(margin, h.x0), cx1 = std::min(1.0 - margin, h.x1);
                double cy0 = std::max(margin, h.y0), cy1 = std::min(1.0 - margin, h.y1);
                if (cx1 - cx0 > 2.0 * margin + 1.0e-3 && cy1 - cy0 > 2.0 * margin + 1.0e-3) {
                    ws.active_holes.push_back({cx0, cx1, cy0, cy1});
                }
            }
        }
    }

    out_places.resize(unit_preps.size());
    return pack_unit_subset(
        ordered, ws, unit_preps, cache, scale, margin, m_sq, mode, max_vars, raw_first, compare_all_variants,
        stop_flag, deadline, out_places, env_w, env_h, env_c, env_r, out_fail_pos, out_fail_uid
    );
}

bool try_uniform_scale_up(
    const std::vector<UnitPrep>& unit_preps,
    std::vector<PlacedUnit>& places,
    double& scale,
    double margin,
    double m_sq
) {
    double max_u = margin, max_v = margin;
    for (size_t uid = 0; uid < unit_preps.size(); ++uid) {
        const auto& pl = places[uid];
        const auto& var = unit_preps[uid].variants[pl.var_idx];
        max_u = std::max(max_u, pl.px + var.width * scale);
        max_v = std::max(max_v, pl.py + var.height * scale);
    }
    double span = std::max(max_u - margin, max_v - margin);
    double usable = 1.0 - 2.0 * margin - 1.5e-5;
    if (span <= 1.0e-6 || span >= usable - 1.0e-4) return false;
    double base_ratio = usable / span;

    for (double factor : {1.0, 0.9985, 0.996}) {
        double ratio = base_ratio * factor;
        if (ratio <= 1.0 + 1.0e-4) continue;
        double cand_scale = scale * ratio;
        SpatialHash idx;
        bool ok = true;
        std::vector<PlacedUnit> cand_places(places.size());
        for (size_t uid = 0; uid < unit_preps.size(); ++uid) {
            const auto& pl = places[uid];
            const auto& var = unit_preps[uid].variants[pl.var_idx];
            double nx = margin + (pl.px - margin) * ratio;
            double ny = margin + (pl.py - margin) * ratio;
            if (variant_conflicts(var, cand_scale, nx, ny, idx, margin, m_sq)) {
                ok = false;
                break;
            }
            insert_shape_into_index(idx, var, cand_scale, nx, ny);
            cand_places[uid] = {nx, ny, pl.var_idx};
        }
        if (ok) {
            scale = cand_scale;
            places = std::move(cand_places);
            return true;
        }
    }
    return false;
}

// Global coordinate squeeze + multi-pass gravity compaction + bottleneck relocate + uniform scale-up!
void squeeze_and_scale_up(
    const std::vector<UnitPrep>& unit_preps,
    const std::vector<ShapeLoops>& static_shapes,
    std::vector<PlacedUnit>& places,
    double& scale,
    double margin,
    double m_sq
) {
    if (!static_shapes.empty() || unit_preps.empty()) return;
    try_uniform_scale_up(unit_preps, places, scale, margin, m_sq);

    const int n = static_cast<int>(unit_preps.size());
    auto build_indexed_hash = [&](SpatialHash& idx, std::vector<std::pair<int, int>>& ranges) {
        idx.clear();
        ranges.resize(n);
        for (int uid = 0; uid < n; ++uid) {
            int start = static_cast<int>(idx.entries.size());
            const auto& var = unit_preps[uid].variants[places[uid].var_idx];
            insert_shape_into_index(idx, var, scale, places[uid].px, places[uid].py, uid);
            ranges[uid] = {start, static_cast<int>(idx.entries.size()) - start};
        }
    };
    auto update_unit_hash = [&](SpatialHash& idx, const std::vector<std::pair<int, int>>& ranges, int uid) {
        const auto [start, cnt] = ranges[uid];
        const auto& var = unit_preps[uid].variants[places[uid].var_idx];
        for (int si = 0; si < cnt; ++si) {
            const auto& sh_loc = var.member_shapes[si];
            ShapeLoops tsh(sh_loc.size());
            for (size_t pi = 0; pi < sh_loc.size(); ++pi) {
                tsh[pi].resize(sh_loc[pi].size());
                for (size_t vi = 0; vi < sh_loc[pi].size(); ++vi) {
                    tsh[pi][vi] = {
                        sh_loc[pi][vi].x * scale + places[uid].px,
                        sh_loc[pi][vi].y * scale + places[uid].py
                    };
                }
            }
            BBox tb = bounds_of_shape(tsh);
            ShapeEdges te = build_shape_edges(tsh);
            idx.entries[start + si] = {std::move(tsh), tb, std::move(te), uid};
        }
        idx.rebuild_buckets();
    };

    // 2 passes of full-pack bottom-left / left-bottom gravity compaction
    std::vector<int> order(n);
    std::iota(order.begin(), order.end(), 0);
    SpatialHash idx;
    std::vector<std::pair<int, int>> ranges;
    for (int pass = 0; pass < 2; ++pass) {
        build_indexed_hash(idx, ranges);
        SweepMode pmode = (pass == 0) ? SweepMode::BL_BOT : SweepMode::LB_LEFT;
        std::sort(order.begin(), order.end(), [&](int a, int b) {
            double sa = places[a].px + places[a].py;
            double sb = places[b].px + places[b].py;
            if (sa != sb) return sa < sb;
            return (pass == 0) ? (places[a].py < places[b].py) : (places[a].px < places[b].px);
        });
        for (int uid : order) {
            const auto& var = unit_preps[uid].variants[places[uid].var_idx];
            double qx = places[uid].px, qy = places[uid].py;
            compact_dir(var, scale, qx, qy, idx, margin, m_sq, pmode, uid);
            if (qx != places[uid].px || qy != places[uid].py) {
                places[uid].px = qx;
                places[uid].py = qy;
                update_unit_hash(idx, ranges, uid);
            }
        }
        try_uniform_scale_up(unit_preps, places, scale, margin, m_sq);
    }

    // Bottleneck relocate-and-rotate loop (up to 4 rounds)
    constexpr int kN = 320;
    constexpr int kW = 5;
    constexpr uint64_t kLastMask = ~0ULL;
    const double inv_n = 1.0 / kN;
    const int mc = std::max(0, static_cast<int>(std::floor(margin * kN)));
    const double nd = std::max(margin + 2.0e-4, 0.75 * inv_n);
    const std::array<Pt, 8> nudges_raw{{{0, 0}, {nd, 0}, {0, nd}, {-nd, 0}, {0, -nd}, {nd, nd}, {nd, -nd}, {-nd, nd}}};
    WorkerWorkspace ws;
    ws.init(kN);

    for (int round = 0; round < 4; ++round) {
        std::vector<double> uspans(n);
        double old_span = margin;
        for (int uid = 0; uid < n; ++uid) {
            const auto& var = unit_preps[uid].variants[places[uid].var_idx];
            double sp = std::max(places[uid].px + var.width * scale, places[uid].py + var.height * scale);
            uspans[uid] = sp;
            if (sp > old_span) old_span = sp;
        }
        std::iota(order.begin(), order.end(), 0);
        std::sort(order.begin(), order.end(), [&](int a, int b) { return uspans[a] > uspans[b]; });
        const int K = std::min(n, 4);
        if (K == 0) break;
        std::vector<int> top_k(order.begin(), order.begin() + K);
        std::vector<uint8_t> in_topk(n, 0);
        double old_k_sum = 0.0;
        for (int uid : top_k) {
            in_topk[uid] = 1;
            old_k_sum += uspans[uid];
        }
        double other_span = margin;
        for (int uid = 0; uid < n; ++uid) {
            if (!in_topk[uid] && uspans[uid] > other_span) other_span = uspans[uid];
        }
        std::sort(top_k.begin(), top_k.end(), [&](int a, int b) {
            return unit_preps[a].hull_area > unit_preps[b].hull_area;
        });

        ws.reset_attempt(kN, kW, n + 16);
        for (int oj = 0; oj < n; ++oj) {
            if (in_topk[oj]) continue;
            const auto& ovar = unit_preps[oj].variants[places[oj].var_idx];
            insert_shape_into_index(ws.index, ovar, scale, places[oj].px, places[oj].py, oj);
            auto sr = rasterize_shapes_center(ovar.member_shapes, scale, places[oj].px, places[oj].py, kN, kN, kN);
            insert_board_rows(sr, kN, 0, 0, kN, kW, 0, kLastMask, ws.rows_raw, ws.rows_pad, ws.rver_raw, ws.rver_pad);
        }

        std::vector<PlacedUnit> cand_k(K);
        bool round_ok = true;
        double max_k_span = margin;
        double new_k_sum = 0.0;
        BitRow fc{}, vcm{}, tmp_sm{}, prev_fc{};
        std::array<int, 32> cols_buf{};

        for (int ki = 0; ki < K; ++ki) {
            int uid = top_k[ki];
            const auto& up = unit_preps[uid];
            const int vcap = std::min<int>(24, static_cast<int>(up.variants.size()));
            ws.bm_hits.clear();

            for (int vi = 0; vi < vcap; ++vi) {
                const auto& var = up.variants[vi];
                const double vw = var.width * scale, vh = var.height * scale;
                const double limit = 1.0 - 2.0 * margin + kEps;
                if (vw > limit || vh > limit) continue;
                int pw = std::min(kN, std::max(1, static_cast<int>(std::ceil(vw * kN - 0.05))));
                int ph = std::min(kN, std::max(1, static_cast<int>(std::ceil(vh * kN - 0.05))));
                int pwords = (pw + 63) >> 6;
                auto prows = rasterize_shapes_center(var.member_shapes, scale, 0.0, 0.0, kN, ph, pw);
                std::vector<PieceRowRuns> rruns;
                rruns.reserve(ph);
                for (int r = 0; r < ph; ++r) {
                    if (row_any(prows[r], pwords)) rruns.push_back({r, extract_runs(prows[r], pwords)});
                }
                if (rruns.empty()) rruns.push_back({0, {{0, 1}}});
                std::sort(rruns.begin(), rruns.end(), [](const PieceRowRuns& a, const PieceRowRuns& b) {
                    int ma = 0, mb = 0;
                    for (const auto& r : a.runs) if (r.len > ma) ma = r.len;
                    for (const auto& r : b.runs) if (r.len > mb) mb = r.len;
                    return ma > mb;
                });

                const int r_lo = mc, r_hi = kN - mc - ph;
                const int c_lo = mc, c_hi = kN - mc - pw;
                if (r_hi < r_lo || c_hi < c_lo) continue;
                const double hx = std::max(margin, 1.0 - margin - vw);
                const double hy = std::max(margin, 1.0 - margin - vh);
                row_clear(vcm, kW);
                row_set_range(vcm, c_lo, c_hi);
                row_clear(prev_fc, kW);
                ws.vhits.clear();

                for (int r0 = r_lo; r0 <= r_hi; ++r0) {
                    fc = vcm;
                    for (const auto& rr_item : rruns) {
                        int rr = r0 + rr_item.dr;
                        const BitRow& brow = ws.rows_raw[rr];
                        if (!row_any(brow, kW)) continue;
                        int rv = ws.rver_raw[rr];
                        for (const auto& pr : rr_item.runs) {
                            const BitRow& sm = ws.get_smeared(rr, pr.len, false, brow, rv, kW);
                            row_shift_right(tmp_sm, sm, pr.dx, kW);
                            for (int w = 0; w < kW; ++w) fc[w] &= ~tmp_sm[w];
                            if (!row_any(fc, kW)) break;
                        }
                        if (!row_any(fc, kW)) break;
                    }
                    if (!row_any(fc, kW)) {
                        row_clear(prev_fc, kW);
                        continue;
                    }
                    int ncols = extract_corner_cols(fc, prev_fc, kW, c_lo, c_hi, true, SweepMode::ENV, cols_buf, false);
                    for (int ci = 0; ci < ncols; ++ci) {
                        int c0 = cols_buf[ci];
                        double tx = std::min(std::max(margin, c0 * inv_n), hx);
                        double ty = std::min(std::max(margin, r0 * inv_n), hy);
                        double k0 = std::max(tx + vw, ty + vh);
                        ws.vhits.push_back({k0, tx + ty, 0.0, 0.0, tx, ty, vi, nullptr});
                    }
                    prev_fc = fc;
                }
                if (!ws.vhits.empty()) {
                    std::sort(ws.vhits.begin(), ws.vhits.end());
                    int keep = std::min<int>(8, static_cast<int>(ws.vhits.size()));
                    ws.bm_hits.insert(ws.bm_hits.end(), ws.vhits.begin(), ws.vhits.begin() + keep);
                }
            }

            if (ws.bm_hits.empty()) { round_ok = false; break; }
            std::sort(ws.bm_hits.begin(), ws.bm_hits.end());

            bool found_k = false;
            double best_sp = kInf, best_sum = kInf, best_qx = 0.0, best_qy = 0.0;
            int best_vi = -1;
            int valid_tested = 0;
            const int max_cand = std::min<int>(48, static_cast<int>(ws.bm_hits.size()));
            for (int ci = 0; ci < max_cand; ++ci) {
                const CandHit& ch = ws.bm_hits[ci];
                const auto& var = up.variants[ch.var_idx];
                const double vw = var.width * scale, vh = var.height * scale;
                const double hx = std::max(margin, 1.0 - margin - vw);
                const double hy = std::max(margin, 1.0 - margin - vh);
                for (const auto& nd_pt : nudges_raw) {
                    double qx0 = std::min(std::max(margin, ch.tx + nd_pt.x), hx);
                    double qy0 = std::min(std::max(margin, ch.ty + nd_pt.y), hy);
                    if (variant_conflicts(var, scale, qx0, qy0, ws.index, margin, m_sq)) continue;
                    double qx1 = qx0, qy1 = qy0;
                    compact_dir(var, scale, qx1, qy1, ws.index, margin, m_sq, SweepMode::BL_BOT);
                    double sp1 = std::max(qx1 + vw, qy1 + vh);
                    double qx2 = qx0, qy2 = qy0;
                    compact_dir(var, scale, qx2, qy2, ws.index, margin, m_sq, SweepMode::LB_LEFT);
                    double sp2 = std::max(qx2 + vw, qy2 + vh);
                    double qx = (sp2 < sp1 || (sp2 == sp1 && qx2 + qy2 < qx1 + qy1)) ? qx2 : qx1;
                    double qy = (sp2 < sp1 || (sp2 == sp1 && qx2 + qy2 < qx1 + qy1)) ? qy2 : qy1;
                    double sp = std::min(sp1, sp2);
                    double ssum = qx + qy;
                    if (!found_k || sp < best_sp - 1.0e-7 || (std::abs(sp - best_sp) <= 1.0e-7 && ssum < best_sum)) {
                        found_k = true;
                        best_sp = sp; best_sum = ssum;
                        best_qx = qx; best_qy = qy; best_vi = ch.var_idx;
                    }
                    ++valid_tested;
                    break;
                }
                if (valid_tested >= 10) break;
            }
            if (!found_k) { round_ok = false; break; }
            cand_k[ki] = {best_qx, best_qy, best_vi};
            if (best_sp > max_k_span) max_k_span = best_sp;
            new_k_sum += best_sp;

            const auto& win_var = up.variants[best_vi];
            insert_shape_into_index(ws.index, win_var, scale, best_qx, best_qy, uid);
            auto sr = rasterize_shapes_center(win_var.member_shapes, scale, best_qx, best_qy, kN, kN, kN);
            insert_board_rows(sr, kN, 0, 0, kN, kW, 0, kLastMask, ws.rows_raw, ws.rows_pad, ws.rver_raw, ws.rver_pad);
        }

        if (!round_ok) break;
        double new_span = std::max(other_span, max_k_span);
        if (new_span < old_span - 1.5e-4 || (new_span <= old_span + 1.0e-6 && new_k_sum < old_k_sum - 2.0e-3)) {
            for (int ki = 0; ki < K; ++ki) places[top_k[ki]] = cand_k[ki];
            try_uniform_scale_up(unit_preps, places, scale, margin, m_sq);
        } else {
            break;
        }
    }
}

ScaleCache build_scale_cache(
    const std::vector<UnitPrep>& unit_preps, double scale, double margin, int grid_n, int num_threads
) {
    ScaleCache sc;
    sc.n = grid_n;
    sc.words = (grid_n + 63) >> 6;
    const int rem_bits = grid_n & 63;
    sc.last_word_mask = (rem_bits == 0) ? ~0ULL : ((1ULL << rem_bits) - 1ULL);
    const int nu = static_cast<int>(unit_preps.size());
    sc.unit_pieces.resize(nu);
    const double limit = 1.0 - 2.0 * margin + kEps;

    std::atomic<int> next_uid{0};
    int tc = std::min(std::max(1, num_threads), std::max(1, nu));
    auto worker = [&]() {
        while (true) {
            int uid = next_uid.fetch_add(1, std::memory_order_relaxed);
            if (uid >= nu) break;
            const auto& up = unit_preps[uid];
            auto& out_vec = sc.unit_pieces[uid];
            std::vector<uint64_t> seen_hashes;
            for (int vi = 0; vi < static_cast<int>(up.variants.size()); ++vi) {
                const auto& var = up.variants[vi];
                double vw = var.width * scale, vh = var.height * scale;
                if (vw > limit || vh > limit) continue;
                int pw = std::min(grid_n, std::max(1, static_cast<int>(std::ceil(vw * grid_n - 0.05))));
                int ph = std::min(grid_n, std::max(1, static_cast<int>(std::ceil(vh * grid_n - 0.05))));
                int pwords = (pw + 63) >> 6;
                auto prows = rasterize_shapes_center(var.member_shapes, scale, 0.0, 0.0, grid_n, ph, pw);

                // Deduplicate identical raster masks across symmetries
                uint64_t h = (static_cast<uint64_t>(pw) << 32) ^ static_cast<uint64_t>(ph);
                int stride = std::max(1, ph / 8);
                for (int r = 0; r < ph; r += stride) {
                    for (int w = 0; w < pwords; ++w) {
                        h ^= prows[r][w] + 0x9e3779b97f4a7c15ULL + (h << 6) + (h >> 2);
                    }
                }
                if (!out_vec.empty() && std::find(seen_hashes.begin(), seen_hashes.end(), h) != seen_hashes.end()) {
                    continue;
                }
                seen_hashes.push_back(h);

                std::vector<PieceRowRuns> rruns;
                rruns.reserve(ph);
                for (int r = 0; r < ph; ++r) {
                    if (row_any(prows[r], pwords)) {
                        rruns.push_back({r, extract_runs(prows[r], pwords)});
                    }
                }
                if (rruns.empty()) {
                    prows[0][0] = 1ULL;
                    rruns.push_back({0, {{0, 1}}});
                }
                // Sort rows by max run length descending for fastest early bitset rejection
                std::sort(rruns.begin(), rruns.end(), [](const PieceRowRuns& a, const PieceRowRuns& b) {
                    int ma = 0, mb = 0;
                    for (const auto& r : a.runs) if (r.len > ma) ma = r.len;
                    for (const auto& r : b.runs) if (r.len > mb) mb = r.len;
                    return ma > mb;
                });

                std::vector<HoleRect> sholes;
                for (const auto& hr : var.inner_holes) {
                    sholes.push_back({hr.x0 * scale, hr.x1 * scale, hr.y0 * scale, hr.y1 * scale});
                }
                if (up.solidity < 0.78) {
                    auto bays = extract_bay_holes(prows, pw, ph, grid_n, pwords);
                    for (const auto& bh : bays) {
                        bool dup = false;
                        for (const auto& ex : sholes) {
                            if (std::abs(bh.x0 - ex.x0) < 0.015 && std::abs(bh.y0 - ex.y0) < 0.015 &&
                                std::abs(bh.x1 - ex.x1) < 0.015 && std::abs(bh.y1 - ex.y1) < 0.015) {
                                dup = true; break;
                            }
                        }
                        if (!dup) sholes.push_back(bh);
                    }
                }
                out_vec.push_back({vi, pw, ph, std::move(prows), std::move(rruns), std::move(sholes)});
            }
            if (out_vec.size() > 1) {
                const double thresh = up.min_var_box * 1.18;
                std::stable_sort(out_vec.begin(), out_vec.end(), [&](const CachedPiece& a, const CachedPiece& b) {
                    const auto& va = up.variants[a.var_idx];
                    const auto& vb = up.variants[b.var_idx];
                    double ba = va.width * va.height;
                    double bb = vb.width * vb.height;
                    bool pa = (ba <= thresh);
                    bool pb = (bb <= thresh);
                    if (pa != pb) return pa;
                    return ba < bb;
                });
            }
        }
    };
    if (tc <= 1) {
        worker();
    } else {
        std::vector<std::thread> threads;
        for (int t = 0; t < tc; ++t) threads.emplace_back(worker);
        for (auto& th : threads) th.join();
    }
    return sc;
}

bool parallel_pack_at_scale(
    const std::vector<UnitPrep>& unit_preps,
    const std::vector<ShapeLoops>& static_shapes,
    const std::vector<std::vector<int>>& base_orders,
    double scale,
    double margin,
    int grid_n,
    int num_threads,
    double budget_sec,
    std::vector<PlacedUnit>& best_places
) {
    const double m_sq = (margin > 1.0e-10) ? (margin - kEps) * (margin - kEps) : 0.0;
    ScaleCache cache = build_scale_cache(unit_preps, scale, margin, grid_n, num_threads);
    const int nu = static_cast<int>(unit_preps.size());
    for (int uid = 0; uid < nu; ++uid) {
        if (cache.unit_pieces[uid].empty()) return false;
    }

    auto deadline = std::chrono::steady_clock::now() +
                    std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                        std::chrono::duration<double>(budget_sec));
    std::atomic<bool> found{false};
    std::mutex win_mtx;

    constexpr std::array<SweepMode, 4> kEnvModes{{
        SweepMode::ENV_X, SweepMode::ENV, SweepMode::MINMAX, SweepMode::CONTACT_BL
    }};
    constexpr std::array<SweepMode, 11> kFastModes{{
        SweepMode::BL_BOT, SweepMode::BL_TOP, SweepMode::LB_LEFT, SweepMode::CONTACT_BL,
        SweepMode::ENV_X, SweepMode::ENV, SweepMode::BR_BOT, SweepMode::TL_TOP,
        SweepMode::MINMAX, SweepMode::LB_RIGHT, SweepMode::RADIAL
    }};
    constexpr std::array<SweepMode, 4> kRawModes{{
        SweepMode::ENV_X, SweepMode::CONTACT_BL, SweepMode::BL_BOT, SweepMode::ENV
    }};

    const int tc = std::max(1, num_threads);
    auto worker = [&](int tid) {
        std::mt19937_64 rng(1337ULL + static_cast<uint64_t>(tid) * 7919ULL);
        std::vector<double> weights(nu, 0.0);
        const auto& base0 = base_orders[tid % static_cast<int>(base_orders.size())];
        std::vector<int> base_rank(nu);
        for (int r = 0; r < nu; ++r) base_rank[base0[r]] = r;

        std::vector<int> best_prefix_order = base0;
        int best_prefix_pos = -1;
        std::vector<PlacedUnit> local_places;
        WorkerWorkspace ws;
        ws.init(cache.n);

        const int role = tid % 8;
        int iter = 0;
        while (!found.load(std::memory_order_relaxed) && std::chrono::steady_clock::now() < deadline) {
            std::vector<int> ordered;
            SweepMode mode;
            bool cmp_all = false;
            bool raw_first = false;

            if (role <= 1) {
                cmp_all = true;
                raw_first = false;
                if (iter == 0) {
                    if (tid == 0) { ordered = base_orders[4 % base_orders.size()]; mode = SweepMode::ENV_X; }
                    else if (tid == 1) { ordered = base_orders[6 % base_orders.size()]; mode = SweepMode::ENV_X; }
                    else {
                        ordered = base_orders[(tid / 2 + role) % base_orders.size()];
                        mode = kEnvModes[(tid / 8 + role) % kEnvModes.size()];
                    }
                } else {
                    mode = kEnvModes[(tid + iter) % kEnvModes.size()];
                }
            } else if (role <= 6) {
                cmp_all = false;
                raw_first = false;
                mode = kFastModes[(tid + iter * 3) % kFastModes.size()];
                if (iter == 0) ordered = base_orders[(tid + role) % base_orders.size()];
            } else {
                cmp_all = false;
                raw_first = true;
                mode = kRawModes[(tid / 8 + iter) % kRawModes.size()];
                if (iter == 0) ordered = base_orders[(tid / 8) % base_orders.size()];
            }

            if (ordered.empty()) {
                if (iter < 4 && (tid % 4 == 0)) {
                    ordered = base_orders[(tid + iter) % static_cast<int>(base_orders.size())];
                } else if (best_prefix_pos > nu / 2 && (iter % 3 == 1)) {
                    ordered = best_prefix_order;
                    int swap_lo = std::max(0, best_prefix_pos - 6);
                    int swap_hi = std::min(nu - 1, best_prefix_pos + 2);
                    if (swap_lo < swap_hi && best_prefix_pos < nu) {
                        int target_slot = std::uniform_int_distribution<int>(0, std::max(0, best_prefix_pos / 2))(rng);
                        int fail_u = ordered[best_prefix_pos];
                        ordered.erase(ordered.begin() + best_prefix_pos);
                        ordered.insert(ordered.begin() + target_slot, fail_u);
                    }
                    int n_swaps = 1 + (iter % 4);
                    for (int s = 0; s < n_swaps; ++s) {
                        int a = std::uniform_int_distribution<int>(0, nu - 1)(rng);
                        int b = std::uniform_int_distribution<int>(0, nu - 1)(rng);
                        std::swap(ordered[a], ordered[b]);
                    }
                } else {
                    double jitter = (iter % 2 == 0) ? 0.14 : 0.36;
                    std::vector<std::pair<double, int>> keys(nu);
                    for (int uid = 0; uid < nu; ++uid) {
                        double j = std::uniform_real_distribution<double>(-jitter, jitter)(rng);
                        keys[uid] = {-weights[uid] + base_rank[uid] * (1.0 + j), uid};
                    }
                    std::sort(keys.begin(), keys.end());
                    ordered.resize(nu);
                    for (int i = 0; i < nu; ++i) ordered[i] = keys[i].second;
                }
            }

            int mvars = 24;
            if (cmp_all) mvars = (role == 2) ? 16 : 8;
            else if (iter % 3 == 0 && (tid % 2 == 0)) mvars = 6;
            else if (iter % 3 == 1) mvars = 12;

            int fpos = 0, fuid = -1;
            bool ok = try_pack_once(ordered, ws, unit_preps, static_shapes, cache, scale, margin, m_sq,
                                    mode, mvars, raw_first, cmp_all, found, deadline, local_places, fpos, fuid);
            if (ok) {
                bool expected = false;
                if (found.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
                    std::lock_guard<std::mutex> lk(win_mtx);
                    best_places = std::move(local_places);
                }
                break;
            }
            if (fuid >= 0) {
                weights[fuid] += 5.0 + (nu - fpos) * 0.25;
                if (fpos > best_prefix_pos) {
                    best_prefix_pos = fpos;
                    best_prefix_order = ordered;
                }
            }
            ++iter;
        }
    };

    if (tc <= 1) {
        worker(0);
    } else {
        std::vector<std::thread> threads;
        threads.reserve(tc);
        for (int t = 0; t < tc; ++t) threads.emplace_back(worker, t);
        for (auto& th : threads) th.join();
    }
    return found.load(std::memory_order_acquire);
}

void parallel_lns_polish(
    const std::vector<UnitPrep>& unit_preps,
    const std::vector<ShapeLoops>& static_shapes,
    double upper,
    double margin,
    double m_sq,
    int num_threads,
    std::chrono::steady_clock::time_point hard_deadline,
    double& best_scale,
    std::vector<PlacedUnit>& best_places
) {
    const int nu = static_cast<int>(unit_preps.size());
    if (!static_shapes.empty() || nu < 4 || best_scale <= 0.0 || best_scale >= upper - 1.0e-4) return;

    const int lns_grid_n = (nu <= 96) ? 384 : 256;
    const int tc = std::max(1, num_threads);
    const std::array<Pt, 5> anchors{{
        {margin, margin},
        {1.0 - margin, margin},
        {margin, 1.0 - margin},
        {1.0 - margin, 1.0 - margin},
        {0.5, 0.5}
    }};
    constexpr std::array<SweepMode, 11> kModes{{
        SweepMode::ENV_X, SweepMode::ENV, SweepMode::MINMAX, SweepMode::CONTACT_BL,
        SweepMode::BL_BOT, SweepMode::BL_TOP, SweepMode::LB_LEFT, SweepMode::BR_BOT,
        SweepMode::TL_TOP, SweepMode::LB_RIGHT, SweepMode::RADIAL
    }};
    constexpr std::array<SweepMode, 3> kShrinkModes{{
        SweepMode::MINMAX, SweepMode::ENV_X, SweepMode::ENV
    }};

    auto compute_layout_span_and_top8 = [&](const std::vector<PlacedUnit>& places, double sc, double& top8_sum) -> double {
        double sp = margin;
        std::vector<double> spans;
        spans.reserve(nu);
        for (int uid = 0; uid < nu; ++uid) {
            const auto& var = unit_preps[uid].variants[places[uid].var_idx];
            double u_sp = std::max(places[uid].px + var.width * sc, places[uid].py + var.height * sc);
            spans.push_back(u_sp);
            if (u_sp > sp) sp = u_sp;
        }
        int k8 = std::min(nu, 8);
        std::partial_sort(spans.begin(), spans.begin() + k8, spans.end(), std::greater<double>());
        top8_sum = 0.0;
        for (int i = 0; i < k8; ++i) top8_sum += spans[i];
        return sp;
    };

    double step = 0.005;
    int round_idx = 0;
    int miss_streak = 0;
    while (best_scale < upper - 1.0e-4) {
        double rem = std::chrono::duration<double>(hard_deadline - std::chrono::steady_clock::now()).count();
        if (rem <= 0.06) break;

        const bool run_span_shrink = (round_idx % 2 == 0);
        const double target_scale = run_span_shrink ? best_scale : std::min(upper, best_scale + step);
        ScaleCache cache = build_scale_cache(unit_preps, target_scale, margin, lns_grid_n, tc);
        bool cache_ok = true;
        for (int uid = 0; uid < nu; ++uid) {
            if (cache.unit_pieces[uid].empty()) { cache_ok = false; break; }
        }
        if (!cache_ok) {
            ++round_idx;
            if (!run_span_shrink) {
                ++miss_streak;
                step *= 0.55;
                if (step < 0.0015) {
                    double r2 = std::chrono::duration<double>(hard_deadline - std::chrono::steady_clock::now()).count();
                    if (r2 > 0.14) step = 0.0040;
                    else break;
                }
            }
            continue;
        }

        rem = std::chrono::duration<double>(hard_deadline - std::chrono::steady_clock::now()).count();
        if (rem <= 0.05) break;
        const double burst_sec = std::min(run_span_shrink ? 0.16 : 0.18, rem);
        auto burst_deadline = std::chrono::steady_clock::now() +
                              std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                                  std::chrono::duration<double>(burst_sec));

        double old_top8_sum = 0.0;
        const double old_span = run_span_shrink ? compute_layout_span_and_top8(best_places, best_scale, old_top8_sum) : 1.0;
        const double ratio = run_span_shrink ? 1.0 : (target_scale / best_scale);
        const double low_lim = margin - 1.0e-9;
        const double high_lim = 1.0 - margin + 1.0e-9;
        const int n = cache.n;
        const int words = cache.words;
        const uint64_t last_mask = cache.last_word_mask;
        const int mc = std::max(0, static_cast<int>(std::floor(margin * n)));
        const int pad_cells = (margin > 1.0e-6) ? std::min(4, std::max(1, static_cast<int>(std::llround(margin * n * 0.92)))) : 0;
        const int widen_bonus = std::min(8, (miss_streak / 2) * 3);

        std::atomic<bool> found{false};
        std::mutex win_mtx;
        std::vector<PlacedUnit> win_places;

        auto worker = [&](int tid) {
            std::mt19937_64 rng(9001ULL + static_cast<uint64_t>(tid) * 65537ULL +
                                static_cast<uint64_t>(round_idx) * 104729ULL +
                                static_cast<uint64_t>(std::llround(target_scale * 10000.0)));
            WorkerWorkspace ws;
            ws.init(n);
            std::vector<PlacedUnit> local_places(nu);
            std::vector<uint8_t> in_ruin(nu, 0);
            std::vector<int> ruin_list;
            std::vector<double> map_x(nu), map_y(nu), map_span(nu);
            ruin_list.reserve(nu);

            int iter = 0;
            while (!found.load(std::memory_order_relaxed) && std::chrono::steady_clock::now() < burst_deadline) {
                std::fill(in_ruin.begin(), in_ruin.end(), 0);
                ruin_list.clear();
                std::vector<int> kept_pool;
                kept_pool.reserve(nu);
                int oob_cnt = 0;

                if (run_span_shrink) {
                    std::vector<int> by_sp(nu);
                    std::iota(by_sp.begin(), by_sp.end(), 0);
                    for (int uid = 0; uid < nu; ++uid) {
                        const auto& pl = best_places[uid];
                        const auto& var = unit_preps[uid].variants[pl.var_idx];
                        map_x[uid] = pl.px;
                        map_y[uid] = pl.py;
                        map_span[uid] = std::max(pl.px + var.width * target_scale, pl.py + var.height * target_scale);
                    }
                    std::sort(by_sp.begin(), by_sp.end(), [&](int a, int b) { return map_span[a] > map_span[b]; });
                    // Ruin all bottleneck units near old_span simultaneously so tied walls move together
                    const int max_border = std::min(nu / 2, 10);
                    const int min_border = std::min(nu, 3);
                    double cx_sum = 0.0, cy_sum = 0.0;
                    for (int k = 0; k < nu && static_cast<int>(ruin_list.size()) < max_border; ++k) {
                        int uid = by_sp[k];
                        if (k >= min_border && map_span[uid] < old_span - 0.015) break;
                        in_ruin[uid] = 1;
                        ruin_list.push_back(uid);
                        const auto& vv = unit_preps[uid].variants[best_places[uid].var_idx];
                        cx_sum += map_x[uid] + 0.5 * vv.width * target_scale;
                        cy_sum += map_y[uid] + 0.5 * vv.height * target_scale;
                    }
                    for (int k = static_cast<int>(ruin_list.size()); k < nu; ++k) kept_pool.push_back(by_sp[k]);

                    const int extra_cnt = std::min<int>(static_cast<int>(kept_pool.size()),
                                                        std::clamp(nu / 5, 4, 8) + widen_bonus / 2);
                    if (extra_cnt > 0 && !ruin_list.empty()) {
                        if ((iter % 3) != 2) {
                            double bx = cx_sum / ruin_list.size();
                            double by = cy_sum / ruin_list.size();
                            std::sort(kept_pool.begin(), kept_pool.end(), [&](int a, int b) {
                                const auto& va = unit_preps[a].variants[best_places[a].var_idx];
                                const auto& vb = unit_preps[b].variants[best_places[b].var_idx];
                                double da = std::hypot((map_x[a] + 0.5 * va.width * target_scale) - bx,
                                                       (map_y[a] + 0.5 * va.height * target_scale) - by);
                                double db = std::hypot((map_x[b] + 0.5 * vb.width * target_scale) - bx,
                                                       (map_y[b] + 0.5 * vb.height * target_scale) - by);
                                return da < db;
                            });
                            int pool_h = std::min<int>(static_cast<int>(kept_pool.size()), extra_cnt * 2 + 2);
                            std::shuffle(kept_pool.begin(), kept_pool.begin() + pool_h, rng);
                        } else {
                            std::shuffle(kept_pool.begin(), kept_pool.end(), rng);
                        }
                        for (int k = 0; k < extra_cnt; ++k) {
                            in_ruin[kept_pool[k]] = 1;
                            ruin_list.push_back(kept_pool[k]);
                        }
                    }
                } else {
                    int anchor_idx = (tid + iter) % static_cast<int>(anchors.size());
                    auto eval_anchor = [&](const Pt& anc, std::vector<uint8_t>& out_oob) -> int {
                        int oob = 0;
                        for (int uid = 0; uid < nu; ++uid) {
                            const auto& pl = best_places[uid];
                            const auto& var = unit_preps[uid].variants[pl.var_idx];
                            double nx = anc.x + ratio * (pl.px - anc.x);
                            double ny = anc.y + ratio * (pl.py - anc.y);
                            double vw = var.width * target_scale;
                            double vh = var.height * target_scale;
                            bool ok_b = (nx >= low_lim && ny >= low_lim && nx + vw <= high_lim && ny + vh <= high_lim);
                            out_oob[uid] = ok_b ? 0 : 1;
                            if (!ok_b) ++oob;
                        }
                        return oob;
                    };

                    oob_cnt = eval_anchor(anchors[anchor_idx], in_ruin);
                    if (oob_cnt > 22) {
                        int best_anc = anchor_idx, best_oob = oob_cnt;
                        std::vector<uint8_t> tmp_oob(nu);
                        for (int ai = 0; ai < static_cast<int>(anchors.size()); ++ai) {
                            int c = eval_anchor(anchors[ai], tmp_oob);
                            if (c < best_oob) { best_oob = c; best_anc = ai; }
                        }
                        anchor_idx = best_anc;
                        oob_cnt = eval_anchor(anchors[anchor_idx], in_ruin);
                    }

                    const Pt anc = anchors[anchor_idx];
                    double oob_cx = 0.0, oob_cy = 0.0;
                    for (int uid = 0; uid < nu; ++uid) {
                        const auto& pl = best_places[uid];
                        const auto& var = unit_preps[uid].variants[pl.var_idx];
                        double nx = anc.x + ratio * (pl.px - anc.x);
                        double ny = anc.y + ratio * (pl.py - anc.y);
                        map_x[uid] = nx;
                        map_y[uid] = ny;
                        map_span[uid] = std::max(nx + var.width * target_scale, ny + var.height * target_scale);
                        if (in_ruin[uid]) {
                            ruin_list.push_back(uid);
                            oob_cx += nx + 0.5 * var.width * target_scale;
                            oob_cy += ny + 0.5 * var.height * target_scale;
                        } else {
                            kept_pool.push_back(uid);
                        }
                    }

                    const int max_extra = (oob_cnt > 24) ? 2 : (9 + widen_bonus);
                    const int min_target = std::clamp(nu / 3, 8 + widen_bonus / 2, 18 + widen_bonus);
                    int need_extra = std::max(4, min_target - oob_cnt);
                    int extra_cnt = std::min<int>({static_cast<int>(kept_pool.size()), max_extra, need_extra});
                    if (extra_cnt > 0) {
                        if ((iter & 1) == 0 && oob_cnt > 0) {
                            // Prefer kept neighbors closest to the OOB boundary cluster
                            double ocx = oob_cx / oob_cnt, ocy = oob_cy / oob_cnt;
                            std::sort(kept_pool.begin(), kept_pool.end(), [&](int a, int b) {
                                const auto& va = unit_preps[a].variants[best_places[a].var_idx];
                                const auto& vb = unit_preps[b].variants[best_places[b].var_idx];
                                double da = std::hypot((map_x[a] + 0.5 * va.width * target_scale) - ocx,
                                                       (map_y[a] + 0.5 * va.height * target_scale) - ocy);
                                double db = std::hypot((map_x[b] + 0.5 * vb.width * target_scale) - ocx,
                                                       (map_y[b] + 0.5 * vb.height * target_scale) - ocy);
                                return da < db;
                            });
                            int pool_h = std::min<int>(static_cast<int>(kept_pool.size()), extra_cnt * 2 + 2);
                            std::shuffle(kept_pool.begin(), kept_pool.begin() + pool_h, rng);
                        } else {
                            std::sort(kept_pool.begin(), kept_pool.end(), [&](int a, int b) {
                                return map_span[a] > map_span[b];
                            });
                            int pool_h = std::min<int>(static_cast<int>(kept_pool.size()), extra_cnt * 2 + 3);
                            std::shuffle(kept_pool.begin(), kept_pool.begin() + pool_h, rng);
                        }
                        for (int k = 0; k < extra_cnt; ++k) {
                            int uid = kept_pool[k];
                            in_ruin[uid] = 1;
                            ruin_list.push_back(uid);
                        }
                    }
                }

                ws.reset_attempt(n, words, nu + 32);
                local_places = best_places;
                double env_w = margin, env_h = margin;
                int env_c = mc, env_r = mc;

                for (int uid = 0; uid < nu; ++uid) {
                    if (in_ruin[uid]) continue;
                    const auto& pl = best_places[uid];
                    const auto& var = unit_preps[uid].variants[pl.var_idx];
                    double nx = std::clamp(map_x[uid], margin, std::max(margin, 1.0 - margin - var.width * target_scale));
                    double ny = std::clamp(map_y[uid], margin, std::max(margin, 1.0 - margin - var.height * target_scale));
                    if (variant_conflicts(var, target_scale, nx, ny, ws.index, margin, m_sq)) {
                        in_ruin[uid] = 1;
                        ruin_list.push_back(uid);
                        continue;
                    }
                    insert_shape_into_index(ws.index, var, target_scale, nx, ny, uid);
                    local_places[uid] = {nx, ny, pl.var_idx};
                    env_w = std::max(env_w, nx + var.width * target_scale);
                    env_h = std::max(env_h, ny + var.height * target_scale);
                    env_c = std::max(env_c, static_cast<int>(std::ceil((nx + var.width * target_scale) * n)));
                    env_r = std::max(env_r, static_cast<int>(std::ceil((ny + var.height * target_scale) * n)));

                    const CachedPiece* matched_cp = nullptr;
                    for (const auto& cp : cache.unit_pieces[uid]) {
                        if (cp.var_idx == pl.var_idx) { matched_cp = &cp; break; }
                    }
                    if (matched_cp != nullptr) {
                        int c0 = std::clamp(static_cast<int>(std::llround(nx * n)), 0, n - 1);
                        int r0 = std::clamp(static_cast<int>(std::llround(ny * n)), 0, n - 1);
                        insert_board_rows(matched_cp->prows, matched_cp->ph, c0, r0, n, words, pad_cells, last_mask,
                                          ws.rows_raw, ws.rows_pad, ws.rver_raw, ws.rver_pad);
                        for (const auto& hr : matched_cp->scaled_holes) {
                            ws.active_holes.push_back({nx + hr.x0, nx + hr.x1, ny + hr.y0, ny + hr.y1});
                        }
                    } else {
                        auto sr = rasterize_shapes_center(var.member_shapes, target_scale, nx, ny, n, n, n);
                        insert_board_rows(sr, n, 0, 0, n, words, pad_cells, last_mask,
                                          ws.rows_raw, ws.rows_pad, ws.rver_raw, ws.rver_pad);
                        for (const auto& hr : var.inner_holes) {
                            ws.active_holes.push_back({
                                nx + hr.x0 * target_scale, nx + hr.x1 * target_scale,
                                ny + hr.y0 * target_scale, ny + hr.y1 * target_scale
                            });
                        }
                    }
                }
                if (ws.active_holes.size() > 24) {
                    std::sort(ws.active_holes.begin(), ws.active_holes.end(), [](const HoleRect& a, const HoleRect& b) {
                        return (a.x1 - a.x0) * (a.y1 - a.y0) > (b.x1 - b.x0) * (b.y1 - b.y0);
                    });
                    ws.active_holes.resize(24);
                }

                std::vector<std::pair<double, int>> rkeys;
                rkeys.reserve(ruin_list.size());
                for (int uid : ruin_list) {
                    double j = std::uniform_real_distribution<double>(0.85, 1.15)(rng);
                    rkeys.push_back({-unit_preps[uid].hull_area * j, uid});
                }
                std::sort(rkeys.begin(), rkeys.end());
                for (size_t i = 0; i < rkeys.size(); ++i) ruin_list[i] = rkeys[i].second;

                SweepMode mode = run_span_shrink
                    ? kShrinkModes[(tid + iter) % kShrinkModes.size()]
                    : kModes[(tid + iter * 3) % kModes.size()];
                const bool raw_first = (((tid + iter) % 3) == 0);
                const bool cmp_all = run_span_shrink ? ((tid & 1) == 0) : ((tid & 3) == 0);
                int fpos = 0, fuid = -1;
                bool ok = ruin_list.empty() || pack_unit_subset(
                    ruin_list, ws, unit_preps, cache, target_scale, margin, m_sq,
                    mode, 12, raw_first, cmp_all, found, burst_deadline,
                    local_places, env_w, env_h, env_c, env_r, fpos, fuid
                );
                if (ok && run_span_shrink) {
                    double new_top8_sum = 0.0;
                    double new_span = compute_layout_span_and_top8(local_places, target_scale, new_top8_sum);
                    if (!(new_span < old_span - 1.5e-4 ||
                          (new_span <= old_span + 1.0e-6 && new_top8_sum < old_top8_sum - 4.0e-3))) {
                        ok = false;
                    }
                }
                if (ok) {
                    bool expected = false;
                    if (found.compare_exchange_strong(expected, true, std::memory_order_acq_rel)) {
                        std::lock_guard<std::mutex> lk(win_mtx);
                        win_places = std::move(local_places);
                    }
                    break;
                }
                ++iter;
            }
        };

        if (tc <= 1) {
            worker(0);
        } else {
            std::vector<std::thread> threads;
            threads.reserve(tc);
            for (int t = 0; t < tc; ++t) threads.emplace_back(worker, t);
            for (auto& th : threads) th.join();
        }

        if (found.load(std::memory_order_acquire)) {
            miss_streak = 0;
            double eff = target_scale;
            squeeze_and_scale_up(unit_preps, static_shapes, win_places, eff, margin, m_sq);
            if (eff > best_scale + 1.0e-4) {
                best_scale = eff;
                best_places = std::move(win_places);
                if (run_span_shrink && eff > target_scale + 5.0e-4) {
                    ++round_idx;
                    continue; // stay on SHRINK next round!
                }
            } else if (run_span_shrink) {
                best_places = std::move(win_places);
            }
            if (!run_span_shrink) step = std::min(0.015, step * 1.25);
        } else if (!run_span_shrink) {
            ++miss_streak;
            step *= 0.55;
            if (step < 0.0015) {
                double r2 = std::chrono::duration<double>(hard_deadline - std::chrono::steady_clock::now()).count();
                if (r2 > 0.14) step = 0.0040;
                else break;
            }
        }
        ++round_idx;
    }
}

inline double compute_convex_hull_area(std::vector<Pt> pts) {
    if (pts.size() < 3) return 0.0;
    std::sort(pts.begin(), pts.end(), [](const Pt& a, const Pt& b) {
        return a.x < b.x || (a.x == b.x && a.y < b.y);
    });
    Poly lower, upper;
    for (const auto& p : pts) {
        while (lower.size() >= 2 && orient(lower[lower.size() - 2], lower.back(), p) <= kEps) lower.pop_back();
        lower.push_back(p);
    }
    for (auto it = pts.rbegin(); it != pts.rend(); ++it) {
        while (upper.size() >= 2 && orient(upper[upper.size() - 2], upper.back(), *it) <= kEps) upper.pop_back();
        upper.push_back(*it);
    }
    if (!lower.empty()) lower.pop_back();
    if (!upper.empty()) upper.pop_back();
    lower.insert(lower.end(), upper.begin(), upper.end());
    return std::abs(signed_area(lower));
}

inline double compute_coverage_pct(
    const std::vector<UnitPrep>& unit_preps,
    const std::vector<PlacedUnit>& places,
    const std::vector<ShapeLoops>& static_shapes,
    double scale
) {
    constexpr int N = 256;
    constexpr int W = N / 64;
    std::vector<BitRow> rows(N);
    for (auto& r : rows) row_clear(r, W);

    for (const auto& sh : static_shapes) {
        BBox b = bounds_of_shape(sh);
        if (b.maxx <= 0.0 || b.minx >= 1.0 || b.maxy <= 0.0 || b.miny >= 1.0) continue;
        auto sr = rasterize_shapes_center({sh}, 1.0, 0.0, 0.0, N, N, N);
        for (int r = 0; r < N; ++r) {
            for (int w = 0; w < W; ++w) rows[r][w] |= sr[r][w];
        }
    }
    for (size_t uid = 0; uid < unit_preps.size(); ++uid) {
        const auto& pl = places[uid];
        const auto& var = unit_preps[uid].variants[pl.var_idx];
        auto sr = rasterize_shapes_center(var.member_shapes, scale, pl.px, pl.py, N, N, N);
        for (int r = 0; r < N; ++r) {
            for (int w = 0; w < W; ++w) rows[r][w] |= sr[r][w];
        }
    }
    int bits = 0;
    for (const auto& r : rows) bits += row_popcount(r, W);
    return 100.0 * bits / (N * N);
}

} // namespace

extern "C" __declspec(dllexport) int uvgpt_pack_solve(
    int num_units,
    const int* unit_shape_offsets,
    const int* shape_loop_offsets,
    const int* loop_vert_offsets,
    const double* verts_x,
    const double* verts_y,
    const double* unit_areas,
    int num_static_shapes,
    const int* static_loop_offsets,
    const int* static_vert_offsets,
    const double* static_x,
    const double* static_y,
    const int* unit_angle_offsets,
    const double* unit_angles_rad,
    double margin,
    int scale_to_fit,
    double min_scale_hint,
    double time_budget_sec,
    int num_threads,
    double* out_scale,
    int* out_angle_idx,
    double* out_tx,
    double* out_ty,
    double* out_coverage_pct
) {
    if (num_units <= 0 || !unit_shape_offsets || !shape_loop_offsets || !loop_vert_offsets ||
        !verts_x || !verts_y || !unit_angle_offsets || !unit_angles_rad ||
        !out_scale || !out_angle_idx || !out_tx || !out_ty) {
        return 0;
    }

    auto t_start = std::chrono::steady_clock::now();
    margin = std::max(0.0, margin);
    int hw_threads = static_cast<int>(std::thread::hardware_concurrency());
    if (num_threads <= 0) num_threads = (hw_threads > 0) ? std::min(32, hw_threads) : 8;
    else num_threads = std::min(32, std::max(1, num_threads));

    // Reconstruct static shapes
    std::vector<ShapeLoops> static_shapes;
    static_shapes.reserve(std::max(0, num_static_shapes));
    for (int si = 0; si < num_static_shapes; ++si) {
        ShapeLoops sh;
        int l0 = static_loop_offsets[si], l1 = static_loop_offsets[si + 1];
        sh.reserve(l1 - l0);
        for (int li = l0; li < l1; ++li) {
            int v0 = static_vert_offsets[li], v1 = static_vert_offsets[li + 1];
            Poly p;
            p.reserve(v1 - v0);
            for (int vi = v0; vi < v1; ++vi) p.push_back({static_x[vi], static_y[vi]});
            sh.push_back(std::move(p));
        }
        static_shapes.push_back(std::move(sh));
    }

    // Build UnitPrep + rotated variants per unit
    std::vector<UnitPrep> unit_preps(num_units);
    std::vector<double> max_var_bounds(num_units, 1.0);
    double total_area = 0.0;
    const double tile_span = std::max(1.0e-9, 1.0 - 2.0 * margin);
    double upper = kInf;

    for (int uid = 0; uid < num_units; ++uid) {
        UnitPrep& up = unit_preps[uid];
        up.area = std::max(1.0e-12, unit_areas ? unit_areas[uid] : 0.0);
        total_area += up.area;

        std::vector<ShapeLoops> centered_shapes;
        std::vector<Pt> all_raw_pts;
        int s0 = unit_shape_offsets[uid], s1 = unit_shape_offsets[uid + 1];
        centered_shapes.reserve(s1 - s0);
        double max_outer = up.area;
        bool has_hole = false;
        for (int si = s0; si < s1; ++si) {
            ShapeLoops sh;
            int l0 = shape_loop_offsets[si], l1 = shape_loop_offsets[si + 1];
            sh.reserve(l1 - l0);
            for (int li = l0; li < l1; ++li) {
                int v0 = loop_vert_offsets[li], v1 = loop_vert_offsets[li + 1];
                Poly p;
                p.reserve(v1 - v0);
                for (int vi = v0; vi < v1; ++vi) {
                    Pt pt{verts_x[vi], verts_y[vi]};
                    all_raw_pts.push_back(pt);
                    p.push_back(pt);
                }
                double pa = std::abs(signed_area(p));
                if (pa > max_outer) max_outer = pa;
                sh.push_back(std::move(p));
            }
            if (sh.size() > 1 && sh[0].size() >= 3) {
                for (size_t k = 1; k < sh.size(); ++k) {
                    if (sh[k].size() >= 3 && std::abs(signed_area(sh[k])) > 1.0e-4 && point_in_polygon(sh[k][0], sh[0])) {
                        has_hole = true;
                        break;
                    }
                }
            }
            centered_shapes.push_back(std::move(sh));
        }
        up.hull_area = std::max(up.area, compute_convex_hull_area(all_raw_pts));
        up.outer_area = std::max(up.area, max_outer);
        up.has_hole = has_hole;
        BBox b0 = bounds_of_pts(all_raw_pts);
        up.max_dim = std::max(b0.maxx - b0.minx, b0.maxy - b0.miny);
        up.min_dim = std::min(b0.maxx - b0.minx, b0.maxy - b0.miny);

        int a0 = unit_angle_offsets[uid], a1 = unit_angle_offsets[uid + 1];
        up.variants.reserve(a1 - a0);
        double unit_scale_cap = 0.0;
        for (int ai = a0; ai < a1; ++ai) {
            double ang = unit_angles_rad[ai];
            double ca = std::cos(ang), sa = std::sin(ang);
            double minx = kInf, maxx = -kInf, miny = kInf, maxy = -kInf;
            std::vector<ShapeLoops> rot_shapes(centered_shapes.size());
            for (size_t si = 0; si < centered_shapes.size(); ++si) {
                rot_shapes[si].resize(centered_shapes[si].size());
                for (size_t li = 0; li < centered_shapes[si].size(); ++li) {
                    const Poly& src_p = centered_shapes[si][li];
                    Poly& dst_p = rot_shapes[si][li];
                    dst_p.resize(src_p.size());
                    for (size_t vi = 0; vi < src_p.size(); ++vi) {
                        double rx = ca * src_p[vi].x - sa * src_p[vi].y;
                        double ry = sa * src_p[vi].x + ca * src_p[vi].y;
                        dst_p[vi] = {rx, ry};
                        if (rx < minx) minx = rx;
                        if (rx > maxx) maxx = rx;
                        if (ry < miny) miny = ry;
                        if (ry > maxy) maxy = ry;
                    }
                }
            }
            if (!std::isfinite(minx)) { minx = maxx = miny = maxy = 0.0; }
            double w = std::max(maxx - minx, 1.0e-10);
            double h = std::max(maxy - miny, 1.0e-10);
            unit_scale_cap = std::max(unit_scale_cap, std::min(tile_span / w, tile_span / h));

            for (auto& sh : rot_shapes) {
                for (auto& poly : sh) {
                    for (auto& pt : poly) { pt.x -= minx; pt.y -= miny; }
                }
            }
            UnitVariant var;
            var.angle_idx = ai - a0;
            var.angle = ang;
            var.width = w;
            var.height = h;
            var.shift_x = minx;
            var.shift_y = miny;
            var.shape_bounds.reserve(rot_shapes.size());
            var.shape_edges.reserve(rot_shapes.size());
            for (const auto& sh : rot_shapes) {
                var.shape_bounds.push_back(bounds_of_shape(sh));
                var.shape_edges.push_back(build_shape_edges(sh));
            }
            var.inner_holes = extract_shape_holes(rot_shapes);
            var.member_shapes = std::move(rot_shapes);
            up.variants.push_back(std::move(var));
        }
        double mv = kInf;
        for (const auto& v : up.variants) mv = std::min(mv, v.width * v.height);
        up.min_var_box = (mv < kInf) ? mv : 1.0;
        up.solidity = std::min(1.0, up.area / std::max(up.hull_area, 1.0e-12));
        upper = std::min(upper, unit_scale_cap);
    }

    if (total_area > 1.0e-16) {
        upper = std::min(upper, std::sqrt((tile_span * tile_span) / total_area) * 1.015);
    }
    if (!std::isfinite(upper) || upper <= 0.0) upper = 1.0;

    // Prebuild diverse static unit orderings
    std::vector<std::vector<int>> base_orders;
    auto make_order = [&](auto cmp) {
        std::vector<int> ord(num_units);
        std::iota(ord.begin(), ord.end(), 0);
        std::sort(ord.begin(), ord.end(), cmp);
        return ord;
    };
    base_orders.push_back(make_order([&](int a, int b) {
        double ma = unit_preps[a].hull_area + 0.18 * unit_preps[a].max_dim * unit_preps[a].max_dim;
        double mb = unit_preps[b].hull_area + 0.18 * unit_preps[b].max_dim * unit_preps[b].max_dim;
        return std::tie(mb, unit_preps[b].max_dim) < std::tie(ma, unit_preps[a].max_dim);
    }));
    base_orders.push_back(make_order([&](int a, int b) {
        return std::tie(unit_preps[b].hull_area, unit_preps[b].max_dim) <
               std::tie(unit_preps[a].hull_area, unit_preps[a].max_dim);
    }));
    base_orders.push_back(make_order([&](int a, int b) {
        double oa = unit_preps[a].outer_area * (unit_preps[a].has_hole ? 1.6 : 1.0);
        double ob = unit_preps[b].outer_area * (unit_preps[b].has_hole ? 1.6 : 1.0);
        return std::tie(ob, unit_preps[b].max_dim) < std::tie(oa, unit_preps[a].max_dim);
    }));
    base_orders.push_back(make_order([&](int a, int b) {
        bool sa = (unit_preps[a].max_dim > 0.28 && unit_preps[a].max_dim / std::max(unit_preps[a].min_dim, 1.0e-9) > 3.2);
        bool sb = (unit_preps[b].max_dim > 0.28 && unit_preps[b].max_dim / std::max(unit_preps[b].min_dim, 1.0e-9) > 3.2);
        if (sa != sb) return sa;
        return std::tie(unit_preps[b].hull_area, unit_preps[b].max_dim) <
               std::tie(unit_preps[a].hull_area, unit_preps[a].max_dim);
    }));
    base_orders.push_back(make_order([&](int a, int b) {
        double ba = unit_preps[a].variants[0].width * unit_preps[a].variants[0].height;
        double bb = unit_preps[b].variants[0].width * unit_preps[b].variants[0].height;
        return std::tie(bb, unit_preps[b].area) < std::tie(ba, unit_preps[a].area);
    }));
    base_orders.push_back(make_order([&](int a, int b) {
        return std::tie(unit_preps[b].max_dim, unit_preps[b].hull_area) <
               std::tie(unit_preps[a].max_dim, unit_preps[a].hull_area);
    }));
    base_orders.push_back(make_order([&](int a, int b) {
        return std::tie(unit_preps[b].area, unit_preps[b].max_dim) <
               std::tie(unit_preps[a].area, unit_preps[a].max_dim);
    }));

    // Adaptive cache-friendly bitset grid size (384 for high-curve <=96, 288 for <=220 149-unit sheets)
    const int grid_n = (num_units <= 96) ? 384 : (num_units <= 220 ? 288 : 256);
    const double m_sq = (margin > 1.0e-10) ? (margin - kEps) * (margin - kEps) : 0.0;
    if (time_budget_sec <= 0.1) time_budget_sec = 3.5;

    std::vector<PlacedUnit> best_places;
    double best_scale = 0.0;
    bool have_best = false;

    if (!scale_to_fit) {
        if (upper >= 1.0 - 1.0e-6) {
            std::vector<PlacedUnit> pl;
            if (parallel_pack_at_scale(unit_preps, static_shapes, base_orders, 1.0, margin,
                                       grid_n, num_threads, time_budget_sec, pl)) {
                best_scale = 1.0;
                best_places = std::move(pl);
                have_best = true;
            }
        }
    } else {
        double low = std::max(0.0, std::min(upper, min_scale_hint));
        // If min_scale_hint > 0, verify it quickly first; otherwise start binary search
        if (low > 1.0e-4) {
            std::vector<PlacedUnit> pl;
            if (parallel_pack_at_scale(unit_preps, static_shapes, base_orders, low, margin,
                                       grid_n, num_threads, std::min(0.45, time_budget_sec * 0.16), pl)) {
                squeeze_and_scale_up(unit_preps, static_shapes, pl, low, margin, m_sq);
                best_scale = low;
                best_places = std::move(pl);
                have_best = true;
            } else {
                low = 0.0;
            }
        }
        double high = std::max(low, upper);
        const int steps = 9;
        const double bs_budget = time_budget_sec * ((num_units <= 96) ? 0.62 : 0.72);
        for (int step = 0; step < steps; ++step) {
            double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();
            double bs_rem = (have_best ? bs_budget : time_budget_sec) - elapsed;
            if (bs_rem < 0.06 || high - low <= 1.2e-3 * std::max(1.0, low)) break;
            double min_sb = (num_units <= 96) ? 0.30 : ((step == 0) ? 0.55 : 0.28);
            double step_budget = std::min(bs_rem, std::max(min_sb, bs_rem / std::max(1, steps - step) * 1.45));
            double probe0 = std::min(high - 0.02, std::max(low + 0.035, 1.0770));
            double mid = (step == 0 && low >= 0.98 && high > 1.11 && probe0 > low + 0.01)
                             ? probe0
                             : ((step == 0 && low < 1.0e-4 && high >= 1.0 && high <= 1.65)
                                    ? 1.0
                                    : (0.5 * (low + high)));
            std::vector<PlacedUnit> pl;
            if (parallel_pack_at_scale(unit_preps, static_shapes, base_orders, mid, margin,
                                       grid_n, num_threads, step_budget, pl)) {
                double eff = mid;
                squeeze_and_scale_up(unit_preps, static_shapes, pl, eff, margin, m_sq);
                if (!have_best || eff > best_scale) {
                    best_scale = eff;
                    best_places = std::move(pl);
                    have_best = true;
                }
                low = std::max(mid, best_scale);
            } else {
                high = mid;
            }
        }
        auto hard_deadline = t_start +
                             std::chrono::duration_cast<std::chrono::steady_clock::duration>(
                                 std::chrono::duration<double>(time_budget_sec));
        if (have_best && static_shapes.empty() && num_units >= 4 && best_scale < upper - 1.0e-4) {
            parallel_lns_polish(unit_preps, static_shapes, upper, margin, m_sq,
                                num_threads, hard_deadline, best_scale, best_places);
        }
        // Fallback probe slightly above best_scale when static blockers bypass LNS
        double elapsed = std::chrono::duration<double>(std::chrono::steady_clock::now() - t_start).count();
        double rem = time_budget_sec - elapsed;
        if (have_best && !static_shapes.empty() && rem > 0.25 && high - best_scale > 2.0e-3) {
            double probe = best_scale + (high - best_scale) * 0.45;
            std::vector<PlacedUnit> pl;
            if (parallel_pack_at_scale(unit_preps, static_shapes, base_orders, probe, margin,
                                       grid_n, num_threads, rem, pl)) {
                double eff = probe;
                squeeze_and_scale_up(unit_preps, static_shapes, pl, eff, margin, m_sq);
                if (eff > best_scale) {
                    best_scale = eff;
                    best_places = std::move(pl);
                }
            }
        }
        if (!have_best && upper > kEps) {
            for (double factor : {0.25, 0.125, 0.05, 0.02}) {
                double psc = upper * factor;
                std::vector<PlacedUnit> pl;
                if (parallel_pack_at_scale(unit_preps, static_shapes, base_orders, psc, margin,
                                           grid_n, num_threads, 0.40, pl)) {
                    best_scale = psc;
                    best_places = std::move(pl);
                    have_best = true;
                    break;
                }
            }
        }
    }

    if (!have_best) return 0;

    *out_scale = best_scale;
    for (int uid = 0; uid < num_units; ++uid) {
        const auto& pl = best_places[uid];
        const auto& var = unit_preps[uid].variants[pl.var_idx];
        out_angle_idx[uid] = var.angle_idx;
        // Return tile position of unit's rotated pivot center so Python converts identically:
        // px_variant = out_tx + scale * py_shift_x
        out_tx[uid] = pl.px - best_scale * var.shift_x;
        out_ty[uid] = pl.py - best_scale * var.shift_y;
    }
    if (out_coverage_pct) {
        *out_coverage_pct = compute_coverage_pct(unit_preps, best_places, static_shapes, best_scale);
    }
    return 1;
}
