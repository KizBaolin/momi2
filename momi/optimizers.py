import autograd
import autograd.numpy as np
import numdifftools
from functools import wraps, partial
from .util import logger, count_calls
import scipy, scipy.optimize
import itertools

def _find_minimum(f, start_params, optimizer, bounds=None,
                  callback=None,
                  opt_kwargs={}, **kwargs):
    fixed_params = []
    if bounds:
        bounds = [(None,None) if b is None else b for b in bounds]
        for i,b in enumerate(bounds):
            try:
                if b[0] == b[1] and b[0] is not None:
                    fixed_params += [(i,b[0])]
            except (TypeError,IndexError) as e:
                fixed_params += [(i,b)]
                    
        if any(start_params[i] != b for i,b in fixed_params):
            raise ValueError("start_params does not agree with fixed parameters in bounds")
        
        if fixed_params:
            fixed_idxs, fixed_offset = list(map(np.array, list(zip(*fixed_params))))

            fixed_idxs = np.array([(i in fixed_idxs) for i in range(len(start_params))])
            proj0 = np.eye(len(fixed_idxs))[:,fixed_idxs]
            proj1 = np.eye(len(fixed_idxs))[:,~fixed_idxs]

            fixed_offset = np.dot(proj0, fixed_offset)
            get_x = lambda x0: np.dot(proj1, x0) + fixed_offset
            def restricted(fun):
                if fun is None: return None
                new_fun = lambda x0, *fargs, **fkwargs: fun(get_x(x0), *fargs, **fkwargs)
                return wraps(fun)(new_fun)
            f = restricted(f)
            callback = restricted(callback)
            
            start_params = np.array([s for (fxd,s) in zip(fixed_idxs,start_params) if not fxd])
            bounds = [b for (fxd,b) in zip(fixed_idxs, bounds) if not fxd]

    opt_kwargs = dict(opt_kwargs)
    assert all([k not in opt_kwargs for k in ['bounds','callback']])
    if callback: opt_kwargs['callback'] = callback
    if bounds and not all([l is None and h is None for (l,h) in bounds]):
        opt_kwargs['bounds'] = bounds

    ret = _find_minimum_helper(f, start_params, optimizer, opt_kwargs, **kwargs)
    if fixed_params:
        ret.x = get_x(ret.x)
    return ret

def _find_minimum_helper(f, start_params, optimizer,
                       opt_kwargs={}, gradmakers={}, replacefun=None):
    opt_kwargs = dict(opt_kwargs)
    for k,v in gradmakers.items():
        assert k not in opt_kwargs
        opt_kwargs[k] = v(f)
    if replacefun:
        f = replacefun(f)
    return optimizer(f, start_params, **opt_kwargs)
             
def nesterov(fun, x0, fun_and_jac, maxiter=1000, bounds=None, callback=None):
    if callback is None:
        callback = lambda *a,**kw:None
    
    if bounds is None:
        bounds = [(None,None)]*len(x0)
    old_bounds, bounds = bounds, []
    for lower, upper in old_bounds:
        if lower is None: lower = -float('inf')
        if upper is None: upper = float('inf')
        bounds.append((lower,upper))
    lower, upper = list(map(np.array, zip(*bounds)))
    def truncate(x): return np.maximum(np.minimum(x, upper), lower)

    fun = count_calls(fun)

    def backtrack_linesearch(y,stepsize,reverse=False):
        ## if reverse==True, do a "forward" line search for maximum stepsize satisfying condition
        fy,gy = fun_and_jac(y)
        while True:
            x = truncate(y - gy*stepsize)
            fx = fun(x)
            if any([not np.isfinite(fz) for fz in (fx,fy)]):
                raise ValueError("Non-finite value of objective function")
            condition = (fx <= fy + 0.5 * np.dot(gy, x-y))
            if reverse: condition = not condition
            if condition: break
            else:
                if reverse: stepsize = 2.0*stepsize
                else: stepsize = 0.5*stepsize
        return x,fx,gy,stepsize
    
    x=np.array(x0,dtype=float)
    y=np.array(x)
    callback(x,fun(x),0)

    ## get initial stepsize
    _,_,_,stepsize = backtrack_linesearch(y,1.0,reverse=True)   
    nesterov_it=0
    for nit in range(1,maxiter+1):
        prev_x = x

        ## update x with backtracking linesearch
        x,fx,gy,stepsize = backtrack_linesearch(y, stepsize)
        callback(x,fx,nit)
        
        ## check convergence
        if np.allclose(y,x):
            success=True
            message="jac(x[k])=~0"
            break        
        if np.allclose(x,prev_x):
            success=True
            message="|x[k]-x[k-1]|=~0"
            break
        
        ## check if restart nesterov
        if np.dot(gy, x - prev_x) > 0:
            nesterov_it=0
            prev_x = x

        ## add nesterov momentum
        y = truncate(x + (nesterov_it)/float(nesterov_it+2)*(x-prev_x))
        nesterov_it+=1

        ## allow stepsize to grow
        stepsize = stepsize * 2.0
    try: success,message
    except NameError:
        success=False
        message="Maximum number of iterations reached"
    return scipy.optimize.OptimizeResult({'success':success, 'message':message,
                                          'nfev':fun.num_calls, 'nit':nit,
                                          'x':x, 'fun':fx, 'jac':gy})


def svrg(fun, x0, fun_and_jac, pieces, iter_per_epoch, maxiter=1000, bounds=None, callback=None, rgen=np.random, delta = .001, gamma = .0001, stepsize=.5, bfgs=True):
    maxstepsize=stepsize
    assert iter_per_epoch <= pieces
    x0 = np.array(x0)
   
    if callback is None:
        callback = lambda *a,**kw:None
        
    if bounds is None:
        bounds = [(None,None) for _ in x0]
    lower,upper = zip(*bounds)
    lower = [-float('inf') if l is None else l
             for l in lower]
    upper = [float('inf') if u is None else u
             for u in upper]
   
    def truncate(x):
        return np.maximum(np.minimum(x, upper), lower)
    def min_qp(z, g, B):
        if not bfgs: return z - g
        
        ret = z - np.linalg.solve(B,g) - gamma*g
        if np.allclose(ret, truncate(ret)): return ret
        H = scipy.linalg.pinvh(B)
        def obj(x):
            return np.dot(x-z,g) + .5 * np.dot(x-z, np.linalg.solve(H + gamma*I, x-z))
        def jac(x):
            return g + np.linalg.solve(H + gamma*I,x-z)
        ret = scipy.optimize.minimize(obj, z, method='tnc', jac=jac, bounds=bounds).x
        assert np.allclose(ret, truncate(ret))
        return truncate(ret)
   
    def backtrack_linesearch(stepsize, f, y, gy, B):
        if B is None: B = I
        assert stepsize <= 1.0
        fy = f(y)
        direction = min_qp(y, gy, B) - y
        while True:
            x = y + stepsize*direction
            if bfgs: assert np.allclose(x, truncate(x))
            else: x = truncate(x)
            fx = f(x)
            if any([not np.isfinite(fz) for fz in (fx,fy)]):
                raise ValueError("Non-finite value of objective function")
            gydxy = np.dot(gy, x-y)
            assert  gydxy < 0.0 or np.isclose(gydxy, 0.0)
            condition = (fx <= fy + 0.5 * np.dot(gy, x-y))
            if condition: break
            else: stepsize = 0.5*stepsize
        return x, stepsize
    
    def update_Hess(B, new_x, prev_x, new_g, prev_g):
        if not bfgs: return None
        
        s = new_x-prev_x
        y = new_g-prev_g
        sy = np.dot(s,y)

        if B is None:
            sy_yy = np.abs(sy) / np.dot(y,y)
            if np.isclose(sy_yy, 0.0): return None
            return np.max((delta, 1.0/sy_yy)) * I
        
        Bs = np.dot(B,s)
        sBs = np.dot(s, Bs)
        if sy >= .2 * sBs:
            theta = 1.
        else:
            theta = .8 * sBs / (sBs - sy)

        r = theta*y + (1.-theta)*Bs
        rho = 1.0/np.dot(r,s)
       
        B = B - np.outer(Bs,Bs)/sBs + np.outer(r,r) / np.dot(s,r) + delta*I
       
        return B

    I = np.eye(len(x0))
    f0,g0 = fun_and_jac(x0, 0)
    #B = delta*I
    B = None
    
    f_avg = pieces*f0
    
    finished = False
    x = x0
    nit = 0
    for epoch in itertools.count():
        if finished: break
        w = x
        x_arr = np.ones((pieces,len(w))) * w

        if nit > 0:
            fbar, gbar = fun_and_jac(w, None)
            f_avg = fbar
        else:
            ## don't do the SVRG step on the first epoch; just do SAGA-like updates
            fbar, gbar = fun_and_jac(w, 0)
            fbar *= pieces
            gbar *= pieces
        
        for k in range(iter_per_epoch):
            if epoch > 0:
                i = rgen.randint(pieces)
                xprev = x_arr[i,:]                
            else:
                ## first epoch (no initial SVRG update)
                i = k
                if i == 0: xprev = w # first iter; no previous x yet
                else: xprev = x_arr[rgen.randint(i),:] # pick random previous x
            
            f_xprev, g_xprev = fun_and_jac(xprev,i)
            f_x, g_x = fun_and_jac(x,i)

            f = pieces*(f_x - f_xprev) + fbar            
            g = pieces*(g_x - g_xprev) + gbar
                       
            ## choose stepsize to ensure Armijo condition
            curr_fun=lambda z: pieces*fun(z,i) + np.dot(z, -pieces*g_xprev + gbar)            
            xnext, stepsize = backtrack_linesearch(maxstepsize, curr_fun, x, g, B)
            assert stepsize <= 1.0
            
            _,g_xnext = fun_and_jac(xnext, i)
            if not np.allclose(x , xnext):
                B = update_Hess(B, xnext, x, pieces*g_xnext, pieces*g_x)

            ## print a running (autoregressive) average of f
            f_avg = f * 1./float(iter_per_epoch) + f_avg * (1.-1./float(iter_per_epoch))
            callback(x, f_avg, nit)
            #logger.debug("stepsize: %f" % stepsize)
            #logger.info("stepsize: %f" % stepsize)
            
            if epoch > 0:
                fbar = fbar + f_x - f_xprev
                gbar = gbar + g_x - g_xprev
            else:
                fbar = (nit*fbar + pieces*f_x) / float(nit+1)
                gbar = (nit*gbar + pieces*g_x) / float(nit+1)
            x_arr[i,:] = x
                       
            if np.allclose(xnext,x) and k==0:
                finished=True
                success=True
                message="|x[k]-x[k-1]|~=0"
                break

            x = xnext
            
            nit += 1
            if nit >= maxiter:
                finished = True
                break

    try: success,message
    except NameError:
        success=False
        message="Maximum number of iterations reached"
    return scipy.optimize.OptimizeResult({'success':success, 'message':message,
                                          'nit':nit,
                                          'x':x, 'fun':fbar, 'jac':gbar})
    
    
custom_opts = {"nesterov": nesterov}
stochastic_opts = {"svrg": svrg}



# def stoch_avg_grad(fun, x0, fun_and_jac, pieces, maxiter=1000, bounds=None, callback=None, rgen=np.random):
#     if callback is None:
#         callback = lambda *a,**kw:None
        
#     if bounds is None:
#         bounds = [(None,None) for _ in x0]
#     lower,upper = zip(*bounds)
#     lower = [-float('inf') if l is None else l
#              for l in lower]
#     upper = [float('inf') if u is None else u
#              for u in upper]

#     curr_funs = np.zeros(pieces)
#     sumfun = 0.0
    
#     curr_grads = np.zeros((pieces, len(x0)))
#     sumgrad = np.zeros(len(x0))
    
#     def truncate(x, truncate_grads=False):
#         ret = np.maximum(np.minimum(x, upper), lower)
#         isntclose = np.logical_not(np.isclose(ret, x))
#         if truncate_grads and np.any(isntclose):
#             curr_grads[:,isntclose] = 0.0
#             sumgrad[isntclose] = 0.0
#         return ret

#     def backtrack_linesearch(y, i, stepsize, reverse=False):
#         ## if reverse==True, do a "forward" line search for maximum stepsize satisfying condition
#         fy,gy = fun_and_jac(y,i)
#         while True:
#             x = truncate(y - gy*stepsize)
#             fx = fun(x,i)
#             if any([not np.isfinite(fz) for fz in (fx,fy)]):
#                 raise ValueError("Non-finite value of objective function")            
#             condition = (fx <= fy + 0.5 * np.dot(gy, x-y))
#             if reverse: condition = not condition
#             if condition: break
#             else:
#                 if reverse: stepsize = 2.0*stepsize
#                 else: stepsize = 0.5*stepsize
#         return (fy,gy), stepsize

#     x = np.array(x0, dtype=float)
#     ## get the initial stepsize
#     _,stepsize = backtrack_linesearch(x, 0, 1.0, reverse=True)

#     ## the initial pass
#     for nit in range(pieces):
#         (f,g),stepsize = backtrack_linesearch(x, nit, stepsize)
#         curr_funs[nit] = f
#         sumfun += f
#         curr_grads[nit,:] = g
#         sumgrad += g
       
#         x = truncate(x - stepsize / float(nit+1) * sumgrad, truncate_grads=True)
#         callback(x, sumfun*float(pieces)/(nit+1.0), nit)        
#         stepsize *= 2.0**(1.0/pieces)

#     ## regular updates
#     finished = False
#     while not finished:
#         #for i in rgen.permutation(pieces):
#         for i in rgen.randint(pieces, size=pieces):
#             nit += 1
#             if nit >= maxiter:
#                 finished = True
#                 break

#             (f,g),stepsize = backtrack_linesearch(x, i, stepsize)

#             sumfun -= curr_funs[i]
#             curr_funs[i] = f
#             sumfun += f

#             ##step = sumgrad + pieces*(g - curr_grads[i,:]) # SAGA update
            
#             sumgrad -= curr_grads[i,:]
#             curr_grads[i,:] = g
#             sumgrad += g

#             prev_x = x
#             x = truncate(x - stepsize / float(pieces) * sumgrad, truncate_grads=True)
#             #x = truncate(x - stepsize * step, truncate_grads=True)
#             callback(x, sumfun, nit)
#             stepsize *= 2.0**(1.0/pieces)

#             if np.allclose(prev_x,x):
#                 finished=True
#                 success=True
#                 message="|x[k]-x[k-1]|~=0"
#                 break

#     try: success,message
#     except NameError:
#         success=False
#         message="Maximum number of iterations reached"
#     return scipy.optimize.OptimizeResult({'success':success, 'message':message,
#                                           'nit':nit,
#                                           'x':x, 'fun':sumfun, 'jac':sumgrad})        

# def adam(fun_and_jac_list, start_params, maxiter, bounds,
#          tol=None,
#          random_generator=np.random, step_size=1., b1=0.9, b2=0.999, eps=10**-8):
#     """Adam as described in http://arxiv.org/pdf/1412.6980.pdf.
#     It's basically RMSprop with momentum and some correction terms."""
#     lower_bounds, upper_bounds = list(zip(*bounds))
#     if tol is not None:
#         raise NotImplementedError("tol not yet implemented")
    
#     x = start_params    
#     m = np.zeros(len(x))
#     v = np.zeros(len(x))

#     step_size = step_size / float(len(fun_and_jac_list))
    
#     history = OptimizeHistory()
#     for curr_pass in range(maxiter):
#         history.new_batch()
#         for i in random_generator.permutation(len(fun_and_jac_list)):
#             fun_and_jac = fun_and_jac_list[i]
            
#             f,g = fun_and_jac(x)
#             history.update(x,f,g)

#             m = (1 - b1) * g      + b1 * m  # First  moment estimate.
#             v = (1 - b2) * (g**2) + b2 * v  # Second moment estimate.
#             mhat = m / float(1 - b1**(i + 1))    # Bias correction.
#             vhat = v / float(1 - b2**(i + 1))
#             x = x-step_size*mhat/(np.sqrt(vhat) + eps)
#             x = np.maximum(np.minimum(x, upper_bounds), lower_bounds)
#     return scipy.optimize.OptimizeResult({'x':x, 'fun':f, 'jac':g, 'history':history})

# def adadelta(fun_and_jac_list, start_params, maxiter, bounds,
#              tol=None,
#              random_generator=np.random, rho=.95, eps=1e-6):
#     lower_bounds, upper_bounds = list(zip(*bounds))
#     if tol is not None:
#         raise NotImplementedError("tol not yet implemented")
    
#     x = start_params    
#     EG2 = 0.
#     EDelX2 = 0.

#     history = OptimizeHistory()
#     for curr_pass in range(maxiter):
#         history.new_batch()
#         for i in random_generator.permutation(len(fun_and_jac_list)):
#             fun,jac = fun_and_jac_list[i]
            
#             f = fun(x)
#             g = jac(x)
#             history.update(x,f,g)

#             EG2 = rho * EG2 + (1.-rho) * (g**2)
#             stepsize = - np.sqrt(EDelX2 + eps) / np.sqrt(EG2 + eps) * g

#             EDelX2 = rho * EDelX2 + (1.-rho) * (stepsize**2)
            
#             x = x-stepsize
#             x = np.maximum(np.minimum(x, upper_bounds), lower_bounds)
#     return scipy.optimize.OptimizeResult({'x':x, 'fun':f, 'jac':g, 'history':history})

# class OptimizeHistory(object):
#     def __init__(self):
#         self.x = []
#         self.f = []
#         self.g = []

#     def new_batch(self):
#         self.x += [[]]
#         self.f += [[]]
#         self.g += [[]]

#     def update(self, x,f,g):
#         self.x[-1] += [x]
#         self.f[-1] += [f]
#         self.g[-1] += [g]