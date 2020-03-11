import fastprogress
import numpy
import logging
import scipy.optimize

from . import core
from . import utils

try:
    import pygmo
except ModuleNotFoundError:
    pygmo = utils.ImportWarner('pygmo')

_log = logging.getLogger('calibr8.optimization')


def _warn_hit_bounds(theta, bounds, theta_names):
    bound_hit = False
    for (ip, p), (lb, ub) in zip(enumerate(theta), bounds):
        pname = f'{ip+1}' if not theta_names else theta_names[ip]
        if numpy.isclose(p, lb):
            _log.warn(f'Parameter {pname} ({p}) is close to its lower bound ({lb}).')
            bound_hit = True
        if numpy.isclose(p, ub):
            _log.warn(f'Parameter {pname} ({p}) is close to its upper bound ({ub}).')
            bound_hit = True
    return bound_hit


def fit_scipy(model:core.ErrorModel, *, independent:numpy.ndarray, dependent:numpy.ndarray, theta_guess:list, theta_bounds:list=None, minimize_kwargs:dict=None):
    """Function to fit the error model with observed data.

    Args:
        independent (array): desired values of the independent variable or measured values of the same
        dependent (array): observations of dependent variable
        theta_guess: initial guess for parameters describing the mode and standard deviation of a PDF of the dependent variable
        theta_bounds: bounds to fit the parameters

    Returns:
        theta: best found parameter vector
        history (list): history of the optimization
    """
    if not minimize_kwargs:
        minimize_kwargs = {}

    history = []
    fit = scipy.optimize.minimize(
        model.objective(independent=independent, dependent=dependent, minimize=True),
        x0=theta_guess,
        bounds=theta_bounds,
        callback=lambda x: history.append(x),
        **minimize_kwargs
    )

    # check for fit success
    if theta_bounds:
        bound_hit = _warn_hit_bounds(fit.x, theta_bounds, model.theta_names)

    if not fit.success or bound_hit:
        _log.warning(f'Fit of {type(model).__name__} has failed:')
        _log.warning(fit)
    model.theta_bounds = theta_bounds
    model.theta_guess = theta_guess
    model.theta_fitted = fit.x
    model.cal_independent = numpy.array(independent)
    model.cal_dependent = numpy.array(dependent)
    return fit.x, history


def fit_pygmo(model:core.ErrorModel, *, independent:numpy.ndarray, dependent:numpy.ndarray, theta_bounds:list, theta_guess:list=None, algos:list=None):
    """Use PyGMO to fit an error model.

    Reference: https://esa.github.io/pygmo2/index.html

    Args:
        model (ErrorModel): the error model that shall be fitted
        independent (array): desired values of the independent variable or measured values of the same
        dependent (array): observations of dependent variable
        theta_bounds: bounds for the model parameters - must not be half-open!
        theta_guess: (optional) initial guess for model parameters

    Returns:
        theta: best found parameter vector
        history (list): history of the optimization
    """
    bounds = tuple(numpy.array(theta_bounds).T)
    
    # problem specification
    objective = model.objective(independent=independent, dependent=dependent, minimize=True)
    class ObjectiveWrapper:
        def get_bounds(self):
            return bounds

        def fitness(self, x):
            return (objective(x),)
    prob = pygmo.problem(ObjectiveWrapper())
    
    # to leverage the full power of PyGMO, we'll use many algorithms at the same time
    algos = [
        pygmo.de1220(gen=30),
        pygmo.pso(gen=30),
        pygmo.simulated_annealing(),
    ] if algos is None else algos

    # for each algorithm there will be one "island" with a "population" of parameter vectors
    # in every "evolution" of the island, the algorithm acts upon the population
    # If there's an initial guess, we'll add it to the population.
    islands = []
    for algo in algos:
        # for DE algorithms, the rule of thumb for population size is ndim*5 to ndim*10
        pop = pygmo.population(prob=prob, size=prob.get_nx() * 10)
        if theta_guess is not None:
            # add initial guess to population
            pop.push_back(theta_guess)
        # create an island where this algorithm rules
        islands.append(pygmo.island(
            algo=algo, pop=pop,
            # islands are parallelized via multiprocessing
            udi=pygmo.islands.mp_island(),
        ))

    # All "islands" are aggregated in an "archipelago".
    # In every "evolution" step, there is "migration" between the populations.
    archipel = pygmo.archipelago(t=pygmo.ring())
    for _island in islands:
        archipel.push_back(_island)
    archipel.wait_check()

    # Run the evolutions and follow the progress
    evolutions = 50
    history = []
    for i in fastprogress.progress_bar(range(evolutions)):
        archipel.evolve(n=1)
        archipel.wait_check()
        history.append(archipel.get_champions_x()[numpy.argmin(archipel.get_champions_f())])
        
    theta_best = archipel.get_champions_x()[numpy.argmin(archipel.get_champions_f())]
    bound_hit = _warn_hit_bounds(theta_best, theta_bounds, model.theta_names)
    if bound_hit:
        _log.warning(f'Bounds were hit during fit of {type(model).__name__} model.')

    model.theta_bounds = theta_bounds
    model.theta_guess = theta_guess
    model.theta_fitted = theta_best
    model.cal_independent = numpy.array(independent)
    model.cal_dependent = numpy.array(dependent)
    return model.theta_fitted, history
