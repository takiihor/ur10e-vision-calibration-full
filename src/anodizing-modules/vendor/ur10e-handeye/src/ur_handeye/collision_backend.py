"""Find separating planes between convex hulls. Verify each plane numerically.

The LP proposes an axis. Only the support gap evaluated on every supplied vertex
can establish separation. A failed solve or a small gap always refuses the path.
"""
import numpy as np
from scipy.optimize import linprog


def hull_gap(a, b):
    # shortcut: Hulls can reject clear concave spaces. Use mesh checks if a verified clear path is refused.
    first = np.column_stack((a, np.ones((len(a), 2))))
    second = np.column_stack((-b, -np.ones(len(b)), np.zeros(len(b))))
    matrix = np.vstack((first, second))
    result = linprog([0, 0, 0, 0, -1], A_ub=matrix, b_ub=np.zeros(len(matrix)),
                     bounds=[(-1, 1)] * 3 + [(None, None), (0, None)], method='highs',
                     options={'time_limit': .5})
    if not result.success:
        return None
    axis = result.x[:3]
    norm = np.linalg.norm(axis)
    if not np.isfinite(norm) or norm < 1e-10:
        return 0.
    axis /= norm
    # Verify the support gap. Solver residuals cannot create clearance.
    gap = float(np.min(b @ axis) - np.max(a @ axis)) - 2e-6
    return gap if np.isfinite(gap) else None
