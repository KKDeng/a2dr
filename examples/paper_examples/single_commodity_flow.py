"""
Copyright 2019 Anqi Fu, Junzi Zhang

This file is part of A2DR.

A2DR is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

A2DR is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with A2DR. If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np
import scipy as sp
import numpy.linalg as LA
import copy
import time
import scipy.sparse.linalg
import matplotlib
matplotlib.use('TKAgg')
import matplotlib.pyplot as plt

from cvxpy import *
from scipy import sparse
from scipy.optimize import nnls
from sklearn.datasets import make_sparse_spd_matrix

from a2dr import a2dr
from a2dr.proximal import *
from a2dr.tests.base_test import BaseTest

class TestPaper(BaseTest):
    """Unit tests for A2DR paper experiments."""

    def setUp(self):
        np.random.seed(1)
        self.eps_rel = 1e-8   # specify these in all examples?
        self.eps_abs = 1e-6
        self.MAX_ITER = 1000

    def test_commodity_flow(self):
        # Problem data.
        p = 300  # Number of sources.
        q = 800  # Number of flows.

        # Construct a random incidence matrix.
        B = sparse.lil_matrix((p,q))
        for i in range(q-p+1):
            idxs = np.random.choice(p, size=2, replace=False)
            tmp = np.random.rand()
            if tmp > 0.5:
                B[idxs[0],i] = 1
                B[idxs[1],i] = -1
            else:
                B[idxs[0],i] = -1
                B[idxs[1],i] = 1
        for j in range(q-p+1,q):
            B[j-(q-p+1),j] = 1
            B[j-(q-p),j] = -1 
        B = sparse.csr_matrix(B)
        
        # Generate source and flow range data
        s_tilde = np.random.randn(p)
        p1, p2, p3 = int(p/3), int(p/3*2), int(p/6*5)
        s_tilde[:p1] = 0
        s_tilde[p1:p2] = -np.abs(s_tilde[p1:p2])
        s_tilde[p2:] = np.sum(np.abs(s_tilde[p1:p2])) / (p-p2)
        L = s_tilde[p1:p2]
        s_max = np.hstack([s_tilde[p2:p3]+0.001, 2*(s_tilde[p3:]+0.001)])
        res = sparse.linalg.lsqr(B, -s_tilde, atol=1e-16, btol=1e-16)
        z_tilde = res[0]
        q1 = int(q/2)
        z_max = np.abs(z_tilde)+0.001
        z_max[q1:] = 2*z_max[q1:]
        
        # Generate cost coefficients
        c = np.random.rand(q)
        d = np.random.rand(p)
        
        # Solve by CVXPY
        z = Variable(q)
        s = Variable(p)
        C = sparse.diags(c)
        D = sparse.diags(d)
        obj = quad_form(z, C) + quad_form(s, D)
        constr = [-z_max<=z, z<=z_max, s[:p1]==0, s[p1:p2]==L, 0<=s[p2:], s[p2:]<=s_max, B*z+s==0]
        prob = Problem(Minimize(obj), constr)
        prob.solve(solver='SCS', eps=self.eps_abs, verbose=True)   # 'OSQP'
        cvxpy_z = z.value
        cvxpy_s = s.value

        # Convert problem to standard form.
        # f_1(z) = \sum_j c_j*z_j^2 + I(-z_max <= z_j <= z_max),
        # f_2(s) = \sum_i d_i*s_i^(source)^2 + I(0 <= s_i^(source) <= s_max) 
        #          + \sum_{i'} I(s_{i'}^{transfer}=0) + \sum_{i''}
        #          + \sum_{i"} I(s_{i"}^{sink}=L_{i"}).
        # A = [B, I], b = 0
        zeros = np.zeros(p1)
        def prox_sat(v, t, c, v_lo = -np.inf, v_hi = np.inf):
            return prox_box_constr(prox_sum_squares(v, t*c), t, v_lo, v_hi)
        prox_list = [lambda v, t: prox_sat(v, t, c, -z_max, z_max),
                     lambda v, t: np.hstack([zeros, L, prox_sat(v[p2:], t, d[p2:], 0, s_max)])]
        A_list = [B, sparse.eye(p)]
        b = np.zeros(p)
        
        # Solve with DRS.
        drs_result = a2dr(prox_list, A_list, b, anderson=False, precond=True, max_iter=2*self.MAX_ITER)
        #drs_result = a2dr(prox_list, A_list, b, anderson=True, precond=True, max_iter=self.MAX_ITER, ada_reg=False)
        #drs_result = a2dr(prox_list, A_list, b, anderson=True, precond=True, max_iter=self.MAX_ITER, ada_reg=False, lam_accel=0)
        #drs_result = a2dr(prox_list, A_list, b, anderson=True, precond=True, max_iter=self.MAX_ITER, ada_reg=False, lam_accel=1e-12)
        print('DRS finished.')
        
        # Solve with A2DR.
        a2dr_result = a2dr(prox_list, A_list, b, anderson=True, precond=True, max_iter=2*self.MAX_ITER)
        #a2dr_result = a2dr(prox_list, A_list, b, anderson=True, precond=True, max_iter=2*self.MAX_ITER, lam_accel=1e-12)
        print('A2DR finished.')
        self.compare_total(drs_result, a2dr_result)
        
        # Check solution correctness
        a2dr_z = a2dr_result['x_vals'][0]
        a2dr_s = a2dr_result['x_vals'][1]
        cvxpy_obj_raw = np.sum(c*cvxpy_z**2) + np.sum(d*cvxpy_s**2)
        a2dr_obj = np.sum(c*a2dr_z**2) + np.sum(d*a2dr_s**2)
        cvxpy_constr_vio = [np.maximum(np.abs(cvxpy_z) - z_max, 0), 
                            cvxpy_s[:p1], 
                            np.abs(cvxpy_s[p1:p2]-L), 
                            np.maximum(-cvxpy_s[p2:],0),
                            np.maximum(cvxpy_s[p2:]-s_max,0),
                            B.dot(cvxpy_z)+cvxpy_s]
        cvxpy_constr_vio_val = np.linalg.norm(np.hstack(cvxpy_constr_vio))
        a2dr_constr_vio = [np.maximum(np.abs(a2dr_z) - z_max, 0), 
                            a2dr_s[:p1], 
                            np.abs(a2dr_s[p1:p2]-L), 
                            np.maximum(-a2dr_s[p2:],0),
                            np.maximum(a2dr_s[p2:]-s_max,0),
                            B.dot(a2dr_z)+a2dr_s]
        a2dr_constr_vio_val = np.linalg.norm(np.hstack(a2dr_constr_vio))
        print('objective cvxpy raw = {}, objective a2dr = {}'.format(cvxpy_obj_raw, a2dr_obj))
        print('constraint violation cvxpy = {}, constraint violation a2dr = {}'.format(
            cvxpy_constr_vio_val, a2dr_constr_vio_val))

if __name__ == '__main__':
    tests = TestPaper()
    tests.setUp()
    tests.test_commodity_flow()

