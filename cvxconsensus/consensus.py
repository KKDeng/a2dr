"""
Copyright 2018 Anqi Fu

This file is part of CVXConsensus.

CVXConsensus is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

CVXConsensus is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with CVXConsensus. If not, see <http://www.gnu.org/licenses/>.
"""

import numpy as np
from time import time
from collections import defaultdict
from multiprocessing import Process, Pipe
import cvxpy.settings as s
from cvxpy.problems.problem import Problem, Minimize
from cvxpy.expressions.constants import Parameter
from cvxpy.atoms import sum_squares

def flip_obj(prob):
	"""Helper function to flip sign of objective function.
	"""
	if isinstance(prob.objective, Minimize):
		return prob.objective
	else:
		return -prob.objective

def assign_rho(p_list, rho_init = dict(), default = 1.0):
	"""Construct dictionaries that map variable id to initial step
	   size value for each problem.
	"""
	return [{var.id: rho_init.get(var.id, default) for var in \
				prob.variables()} for prob in p_list]

# Spectral step size.
def step_ls(p, d):
	"""Least squares estimator for spectral step size.
	
	Parameters
	----------
	p : array
	     Change in primal variable.
	d : array
	     Change in dual variable.
	
	Returns
	----------
	float
	     The least squares estimate.
	"""
	sd = np.sum(d**2)/np.sum(p*d)   # Steepest descent
	mg = np.sum(p*d)/np.sum(p**2)   # Minimum gradient
	
	if 2*mg > sd:
		return mg
	else:
		return (sd - mg/2)

def step_cor(p, d):
	"""Correlation coefficient.
	
	Parameters
	----------
	p : array
	     First vector.
	d : array
	     Second vector.
	
	Returns
	----------
	float
	     The correlation between two vectors.
	"""
	return np.sum(p*d)/(np.linalg.norm(p)*np.linalg.norm(d))

def step_safe(rho, a, b, a_cor, b_cor, eps = 0.2):
	"""Safeguarding rule for spectral step size update.
	
	Parameters
	----------
    rho : float
        The current step size.
    a : float
        Reciprocal of the curvature parameter alpha.
    b : float
        Reciprocal of the curvature parameter beta.
    a_cor : float
        Correlation of the curvature parameter alpha.
    b_cor : float
        Correlation of the curvature parameter beta.
    eps : float, optional
        The safeguarding threshold.
	"""
	if a_cor > eps and b_cor > eps:
		return np.sqrt(a*b)
	elif a_cor > eps and b_cor <= eps:
		return a
	elif a_cor <= eps and b_cor > eps:
		return b
	else:
		return rho

def step_spec(rho, k, dx, dxbar, dy, dyhat, eps = 0.2, C = 1e10):
	"""Calculates the generalized spectral step size with safeguarding.
	Xu, Taylor, et al. "Adaptive Consensus ADMM for Distributed Optimization."
	
	Parameters
    ----------
    rho : float
        The current step size.
    k : int
        The current iteration.
    dx : array
        Change in primal value from the last step size update.
    dxbar : array
        Change in average primal value from the last step size update.
    dy : array
        Change in dual value from the last step size update.
    dyhat : array
        Change in intermediate dual value from the last step size update.
    eps : float, optional
        The safeguarding threshold.
    C : float, optional
        The convergence constant.
    
    Returns
    ----------
    float
        The spectral step size for the next iteration.
	"""
	# Use old step size if unable to solve LS problem/correlations.
	eps_fl = np.finfo(float).eps   # Machine precision.
	if np.sum(dx**2) <= eps_fl or np.sum(dxbar**2) <= eps_fl or \
	   np.sum(dy**2) <= eps_fl or np.sum(dyhat**2) <= eps_fl:
		   return rho

	# Compute spectral step size.
	a_hat = step_ls(dx, dyhat)
	b_hat = step_ls(dxbar, dy)
	
	# Estimate correlations.
	a_cor = step_cor(dx, dyhat)
	b_cor = step_cor(dxbar, dy)
	
	# Apply safeguarding rule.
	scale = 1 + C/(1.0*k**2)
	rho_hat = step_safe(rho, a_hat, b_hat, a_cor, b_cor, eps)
	return max(min(rho_hat, scale*rho), rho/scale)

def prox_step(prob, rho_init, scaled = False, spectral = False):
	"""Formulates the proximal operator for a given objective, constraints, and step size.
	Parikh, Boyd. "Proximal Algorithms."
	
	Parameters
    ----------
    prob : Problem
        The objective and constraints associated with the proximal operator.
        The sign of the objective function is flipped if `prob` is a maximization problem.
    rho_init : float
        The initial step size.
    scaled : logical, optional
    	Should the dual variable be scaled?
	spectral : logical, optional
	    Will spectral step sizes be used?
    
    Returns
    ----------
    prox : Problem
        The proximal step problem.
    vmap : dict
        A map of each proximal variable id to a dictionary containing that variable `x`,
        the mean variable parameter `xbar`, the associated dual parameter `y`, and the
        step size parameter `rho`. If `spectral = True`, the estimated dual parameter 
        `yhat` is also included.
	"""
	vmap = {}   # Store consensus variables
	f = flip_obj(prob).args[0]
	
	# Add penalty for each variable.
	for xvar in prob.variables():
		xid = xvar.id
		shape = xvar.shape
		vmap[xid] = {"x": xvar, "xbar": Parameter(shape, value = np.zeros(shape)),
		 		     "y": Parameter(shape, value = np.zeros(shape)),
					 "rho": Parameter(value = rho_init[xid], nonneg = True)}
		if spectral:
			vmap[xid]["yhat"] = Parameter(shape, value = np.zeros(shape))
		dual = vmap[xid]["y"] if scaled else vmap[xid]["y"]/vmap[xid]["rho"]
		f += (vmap[xid]["rho"]/2.0)*sum_squares(xvar - vmap[xid]["xbar"] + dual)
	
	prox = Problem(Minimize(f), prob.constraints)
	return prox, vmap

def x_average(prox_res):
	"""Average the primal variables over the nodes in which they are present,
	   weighted by each node's step size.
	"""
	xmerge = defaultdict(list)
	rho_sum = defaultdict(float)
	
	for status, rho, xvals in prox_res:
		# Check if proximal step converged.
		if status in s.INF_OR_UNB:
			raise RuntimeError("Proximal problem is infeasible or unbounded")
		
		# Merge dictionary of x values
		for key, value in xvals.items():
			xmerge[key].append(rho[key]*value)
			rho_sum[key] += rho[key]
	
	return {key: np.sum(np.array(xlist), axis = 0)/rho_sum[key] for key, xlist in xmerge.items()}

def res_stop(res_ssq, eps = 1e-4):
	"""Calculate the sum of squared primal/dual residuals.
	   Determine whether the stopping criterion is satisfied:
	   ||r^(k)||^2 <= eps*max(\sum_i ||x_i^(k)||^2, \sum_i ||x_bar^(k)||^2) and
	   ||d^(k)||^2 <= eps*\sum_i ||y_i^(k)||^2
	"""
	primal = np.sum([r["primal"] for r in res_ssq])
	dual = np.sum([r["dual"] for r in res_ssq])
	
	x_ssq = np.sum([r["x"] for r in res_ssq])
	xbar_ssq = np.sum([r["xbar"] for r in res_ssq])
	u_ssq = np.sum([r["y"] for r  in res_ssq])
	
	eps_fl = np.finfo(float).eps   # Machine precision.
	stopped = (primal <= eps*max(x_ssq, xbar_ssq) + eps_fl) and \
			  (dual <= eps*u_ssq + eps_fl)
	return primal, dual, stopped

def run_worker(pipe, p, rho_init, *args, **kwargs):
	# Spectral step size parameters.
	spectral = kwargs.pop("spectral", False)
	Tf = kwargs.pop("Tf", 2)
	eps = kwargs.pop("eps", 0.2)
	C = kwargs.pop("C", 1e10)
	
	# Initiate proximal problem.
	prox, v = prox_step(p, rho_init, spectral = spectral)
	
	# Initiate step size variables.
	if spectral:
		v_old = {key: {"x": np.zeros(vmap["x"].shape), "xbar": np.zeros(vmap["xbar"].shape),
			           "y": np.zeros(vmap["y"].shape), "yhat": np.zeros(vmap["yhat"].shape)} \
			       for key, vmap in v.items()}
	
	# ADMM loop.
	while True:
		# Proximal step for x^(k+1).
		prox.solve(*args, **kwargs)
		
		# Calculate x_bar^(k+1).
		xvals = {}
		for xvar in prox.variables():
			xvals[xvar.id] = xvar.value
		rvals = {key: vmap["rho"].value for key, vmap in v.items()}
		pipe.send((prox.status, rvals, xvals))
		xbars, i = pipe.recv()
		
		# Update y^(k+1) = y^(k) + rho^(k)*(x^(k+1) - x_bar^(k+1)).
		ssq = {"primal": 0, "dual": 0, "x": 0, "xbar": 0, "y": 0}
		for key in v.keys():
			# Calculate residuals for the (k+1) step.
			#    Primal: x^(k+1) - x_bar^(k+1)
			#    Dual: rho^(k+1)*(x_bar^(k) - x_bar^(k+1)) 
			if v[key]["x"].value is None:
				primal = -xbars[key]
			else:
				primal = (v[key]["x"] - xbars[key]).value
			dual = (v[key]["rho"]*(v[key]["xbar"] - xbars[key])).value
			
			# Set parameter values of x_bar^(k+1) and y^(k+1).
			xbar_old = v[key]["xbar"].value
			y_old = v[key]["y"].value
			
			v[key]["xbar"].value = xbars[key]
			v[key]["y"].value += (v[key]["rho"]*(v[key]["x"] - v[key]["xbar"])).value
			
			# Save stopping rule criteria.
			ssq["primal"] += np.sum(np.square(primal))
			ssq["dual"] += np.sum(np.square(dual))
			if v[key]["x"].value is not None:
				ssq["x"] += np.sum(np.square(v[key]["x"].value))
			ssq["xbar"] += np.sum(np.square(v[key]["xbar"].value))
			ssq["y"] += np.sum(np.square(v[key]["y"].value))
			
			# Spectral step size.
			if spectral and i % Tf == 1:
				# Calculate y_hat^(k+1) for step size update.
				v[key]["yhat"] = y_old + v[key]["rho"]*(v[key]["x"] - xbar_old)

				# Calculate change from old iterate.
				dx = v[key]["x"].value - v_old[key]["x"]
				dxbar = -v[key]["xbar"].value + v_old[key]["xbar"]
				dy = v[key]["y"].value - v_old[key]["y"]
				dyhat = v[key]["yhat"].value - v_old[key]["yhat"]
				
				# Update step size.
				v[key]["rho"].value = step_spec(v[key]["rho"].value, i, dx, dxbar, dy, dyhat, eps, C)
				
				# Update step size variables.
				v_old[key]["x"] = v[key]["x"].value
				v_old[key]["xbar"] = v[key]["xbar"].value
				v_old[key]["y"] = v[key]["y"].value
				v_old[key]["yhat"] = v[key]["yhat"].value
		pipe.send(ssq)

def consensus(p_list, *args, **kwargs):
	N = len(p_list)   # Number of problems.
	max_iter = kwargs.pop("max_iter", 100)
	rho_init = kwargs.pop("rho_init", dict())
	if np.isscalar(rho_init):
	    rho_list = assign_rho(p_list, default = rho_init)
	else:
	    rho_list = assign_rho(p_list, rho_init = rho_init)
	eps = kwargs.pop("eps", 1e-6)   # Stopping tolerance.
	resid = np.zeros((max_iter, 2))
	
	# Set up the workers.
	pipes = []
	procs = []
	for i in range(N):
		local, remote = Pipe()
		pipes += [local]
		procs += [Process(target = run_worker, args = (remote, p_list[i], rho_list[i]) + args, kwargs = kwargs)]
		procs[-1].start()

	# ADMM loop.
	start = time()
	for i in range(max_iter):
		# Gather and average x_i.
		prox_res = [pipe.recv() for pipe in pipes]
		xbars = x_average(prox_res)
	
		# Scatter x_bar.
		for pipe in pipes:
			pipe.send((xbars, i))
		
		# Calculate normalized residuals.
		ssq = [pipe.recv() for pipe in pipes]
		primal, dual, stopped = res_stop(ssq, eps)
		resid[i,:] = np.array([primal, dual])
		if stopped:
			break
	end = time()

	[p.terminate() for p in procs]
	return {"xbars": xbars, "residuals": resid[:(i+1),:], "num_iters": i+1, "solve_time": (end - start)}