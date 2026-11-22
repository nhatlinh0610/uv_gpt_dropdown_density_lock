"""Pure tests for the user-facing Pro force-snap planner."""

from __future__ import annotations

import unittest

from uv_gpt import pro_snap
from uv_gpt import topology_correspondence as topology


def polygon_graph(points, face_key=0):
    count = len(points)
    loop_keys = tuple((face_key, index) for index in range(count))
    loops = tuple(
        topology.LoopRecord(
            key=loop_keys[index],
            face_key=face_key,
            edge_key=index,
            vertex_key=index,
            next_key=loop_keys[(index + 1) % count],
            prev_key=loop_keys[(index - 1) % count],
            uv=tuple(points[index]),
            boundary=True,
        )
        for index in range(count)
    )
    edges = tuple(
        topology.EdgeRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            face_keys=(face_key,),
            boundary=True,
        )
        for index in range(count)
    )
    vertices = tuple(
        topology.VertexRecord(
            key=index,
            loop_keys=(loop_keys[index],),
            boundary=True,
        )
        for index in range(count)
    )
    return topology.make_graph(
        faces=(topology.FaceRecord(face_key, loop_keys),),
        edges=edges,
        vertices=vertices,
        loops=loops,
        boundaries=(topology.BoundaryComponentRecord("outer", loop_keys, "outer"),),
    )


class ProSnapTests(unittest.TestCase):
    def test_distorted_same_topology_snaps_to_largest_uv_area_master(self):
        master = polygon_graph(((0.0, 0.0), (4.0, 0.0), (0.0, 2.0)))
        skewed = polygon_graph(((0.0, 0.0), (3.0, 0.2), (0.7, 4.0)))
        stretched = polygon_graph(((0.0, 0.0), (8.0, 0.0), (1.0, 0.4)))
        plan = pro_snap.plan_force_snap(
            (
                pro_snap.SnapIslandRecord("small", 1.0, stretched),
                pro_snap.SnapIslandRecord("master", 9.0, master),
                pro_snap.SnapIslandRecord("middle", 4.0, skewed),
            )
        )
        self.assertEqual(plan.master_keys, ("master",))
        self.assertEqual(
            {pair.candidate_key for pair in plan.pairs},
            {"small", "middle"},
        )
        self.assertTrue(all(pair.master_key == "master" for pair in plan.pairs))
        self.assertTrue(all(pair.correspondence.accepted for pair in plan.pairs))

    def test_different_topology_becomes_separate_group(self):
        triangle = polygon_graph(((0.0, 0.0), (1.0, 0.0), (0.0, 1.0)))
        quad = polygon_graph(((0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)))
        plan = pro_snap.plan_force_snap(
            (
                pro_snap.SnapIslandRecord("tri", 2.0, triangle),
                pro_snap.SnapIslandRecord("quad", 3.0, quad),
            )
        )
        self.assertEqual(set(plan.master_keys), {"tri", "quad"})
        self.assertEqual(plan.pairs, ())


if __name__ == "__main__":
    unittest.main()
