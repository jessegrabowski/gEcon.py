from gEcon.parser import gEcon_parser, file_loaders
from gEcon.parser.parse_distributions import create_prior_distribution_dictionary, build_pymc_model
from gEcon.parser.parse_equations import single_symbol_to_sympy
from gEcon.classes.block import Block
from gEcon.shared.utilities import unpack_keys_and_values, is_variable, sort_dictionary, \
    sympy_keys_to_strings, sympy_number_values_to_floats, sequential, string_keys_to_sympy, \
    merge_dictionaries, expand_subs_for_all_times, substitute_all_equations
from gEcon.shared.dynare_convert import make_all_var_time_combos

from gEcon.classes.time_aware_symbol import TimeAwareSymbol
from gEcon.parser.constants import STEADY_STATE_NAMES
from gEcon.exceptions.exceptions import SteadyStateNotSolvedError, GensysFailedException, VariableNotFoundException, \
    MultipleSteadyStateBlocksException, PerturbationSolutionNotFoundException

from gEcon.solvers.steady_state import SteadyStateSolver
from gEcon.solvers.perturbation import PerturbationSolver

from gEcon.solvers.gensys import interpret_gensys_output, qzdiv
from gEcon.classes.progress_bar import ProgressBar

import numpy as np
import sympy as sp
import pandas as pd
import pymc as pm
import aesara
import aesara.tensor as at

from scipy import linalg, stats
from numba import njit

from warnings import warn
from functools import partial

from numpy.typing import ArrayLike
from typing import List, Dict, Optional, Union, Callable, Any, Tuple

VariableType = Union[sp.Symbol, TimeAwareSymbol]


class gEconModel:

    def __init__(self, model_filepath: str, verbose: bool = True, build_pymc = False) -> None:
        """
        Class to build, debug, and solve a DSGE model from a GCN file.

        Parameters
        ----------
        model_filepath
        verbose
        build_pymc
        """

        self.model_filepath: str = model_filepath

        # Model metadata
        self.options: Optional[Dict[str, bool]] = None
        self.try_reduce_vars: Optional[List[TimeAwareSymbol]] = None

        self.blocks: Dict[str, Block] = {}
        self.n_blocks: int = 0

        # Model components
        self.variables: List[TimeAwareSymbol] = []
        self.shocks: List[TimeAwareSymbol] = []
        self.system_equations: List[sp.Add] = []
        self.calibrating_equations: List[sp.Add] = []
        self.params_to_calibrate: List[sp.Symbol] = []
        self.free_param_dict: Dict[sp.Symbol, float] = {}
        self.calib_param_dict: Dict[sp.Symbol, float] = {}
        self.steady_state_relationships: Dict[VariableType, sp.Add] = {}

        self.param_priors: Dict[str, Any] = {}
        self.shock_priors: Dict[str, Any] = {}

        self.pymc_model: pm.Model = None

        self.n_variables: int = 0
        self.n_shocks: int = 0
        self.n_equations: int = 0
        self.n_calibrating_equations: int = 0

        # Functional representations of the model
        self.f_ss: Union[Callable, None] = None
        self.f_ss_resid: Union[Callable, None] = None

        # Steady state information
        self.steady_state_solved: bool = False
        self.steady_state_system: List[sp.Add] = []
        self.steady_state_dict: Dict[sp.Symbol, float] = {}
        self.residuals: List[float] = []

        # Functional representation of the perturbation system
        self.build_perturbation_matrices: Union[Callable, None] = None

        # Perturbation solution information
        self.perturbation_solved: bool = False
        self.T: pd.DataFrame = None
        self.R: pd.DataFrame = None
        # self.P: pd.DataFrame = None
        # self.Q: pd.DataFrame = None
        # self.R: pd.DataFrame = None
        # self.S: pd.DataFrame = None

        self.build(verbose=verbose, build_pymc=build_pymc)

        # Assign Solvers
        self.steady_state_solver = SteadyStateSolver(self)
        self.perturbation_solver = PerturbationSolver(self)

    def build(self, verbose: bool = True, build_pymc=False) -> None:
        """
        Main parsing function for the model. Build loads the GCN file, decomposes it into blocks, solves optimization
        problems contained in each block, then extracts parameters, equations, calibrating equations, calibrated
        parameters, and exogenous shocks into their respective class attributes.

        Priors declared in the GCN file are converted into scipy distribution objects and stored in two dictionaries:
        self.param_priors and self.shock_priors. In addition, the user can request a PyMC model object be built. If
        you plan to do Bayesian estimation, build the PyMC model.

        Gathering block information is done for convenience. For diagnostic purposes the block structure is retained
        as well.

        Parameters
        ----------
        verbose: bool, default: True
            When true, prints a build report describing the model structure and warning the user if the number of
            variables does not match the number of equations.
        build_pymc_model: bool, default: False
            If true, builds a PyMC model of random variables declared in the GCN and stores it in self.pymc_model.

        Returns
        -------

        """
        raw_model = file_loaders.load_gcn(self.model_filepath)
        parsed_model, prior_dict = gEcon_parser.preprocess_gcn(raw_model)

        self._build_model_blocks(parsed_model)
        self._get_all_block_equations()
        self._get_all_block_parameters()
        self._get_all_block_params_to_calibrate()
        self._get_variables_and_shocks()
        self._build_prior_dict(prior_dict, build_pymc)

        reduced_vars = self._try_reduce()
        singletons = self._simplify_singletons()

        if verbose:
            self.build_report(reduced_vars, singletons)

    def build_report(self, reduced_vars, singletons):
        """
        Write a diagnostic message after building the model. Note that successfully building the model does not
        guarantee that the model is correctly specified. For example, it is possible to build a model with more
        equations than parameters. This message will warn the user in this case.

        Returns
        -------
        None
        """
        if len(singletons) == 0:
            singletons = None

        eq_str = "equation" if self.n_equations == 1 else "equations"
        var_str = "variable" if self.n_variables == 1 else "variables"
        shock_str = "shock" if self.n_shocks == 1 else "shocks"
        cal_eq_str = "equation" if self.n_calibrating_equations == 1 else "equations"
        par_str = "parameter" if self.n_params_to_calibrate == 1 else "parameters"

        n_params = len(self.free_param_dict) + len(self.calib_param_dict)

        param_priors = self.param_priors.keys()
        shock_priors = self.shock_priors.keys()

        report = 'Model Building Complete.\nFound:\n'
        report += f'\t{self.n_equations} {eq_str}\n'
        report += f'\t{self.n_variables} {var_str}\n'

        if reduced_vars:
            report += f'\tThe following variables were eliminated at user request:\n'
            report += f'\t\t' + ','.join(reduced_vars) + '\n'

        if singletons:
            report += f'\tThe following "variables" were defined as constants and have been substituted away:\n'
            report += f'\t\t' + ','.join(singletons) + '\n'

        report += f'\t{self.n_shocks} stochastic {shock_str}\n'
        report += f'\t\t {len(shock_priors)} / {self.n_shocks} {"have" if len(shock_priors) == 1 else "has"}' \
                  f' a defined prior. \n'

        report += f'\t{n_params} {par_str}\n'
        report += f'\t\t {len(param_priors)} / {n_params} {"have" if len(param_priors) == 1 else "has"} ' \
                  f'a defined prior. \n'
        report += f'\t{self.n_calibrating_equations} calibrating {cal_eq_str}\n'
        report += f'\t{self.n_params_to_calibrate} {par_str} to calibrate\n '

        if self.n_equations == self.n_variables:
            report += 'Model appears well defined and ready to proceed to solving.\n'
            print(report)
        else:
            print(report)
            message = f'The model does not appear correctly specified, there are {self.n_equations} {eq_str} but ' \
                      f'{self.n_variables} {var_str}. It will not be possible to solve this model. Please check the ' \
                      f'specification using available diagnostic tools, and check the GCN file for typos.'
            warn(message)

    def steady_state(self,
                     verbose: Optional[bool] = True,
                     optimizer_kwargs: Optional[Dict[str, Any]] = None,
                     param_bounds: Optional[Dict[str, Tuple[float, float]]] = None,
                     use_jac: Optional[bool] = True) -> None:
        """
        Solves for a function f(params) that computes steady state values and calibrated parameter values given
        parameter values, stores results, and verifies that the residuals of the solution are zero.

        Parameters
        ----------
        verbose: bool
            Flag controlling whether to print results of the steady state solver
        optimizer_kwargs: dict
            Dictionary of arguments to be passed to scipy.optimize.root or scipy.optimize.root_scalar, see those
            functions for more details.
        param_bounds: dict
            Dictionary of variable/calibrated parameter names and bounds, currently given to the brentq root finding
            algorithm. If this is None, all bounds will be assigned as (0, 1) if a solver requires bounds.
        use_jac: bool
            Boolean flag, whether to explicitly compute the jacobian function of the steady state. Helpful for solving
            complex systems, but potentially slow.
        Returns
        -------
        None
        """
        if not self.steady_state_solved:
            self.f_ss = self.steady_state_solver.solve_steady_state(optimizer_kwargs=optimizer_kwargs,
                                                                    param_bounds=param_bounds,
                                                                    use_jac=use_jac)

            self.f_ss_resid = self.steady_state_solver.f_ss_resid

        self._process_steady_state_results(verbose)

    def _process_steady_state_results(self, verbose=True) -> None:
        self.steady_state_dict, self.calib_param_dict = self.f_ss(self.free_param_dict)
        self.steady_state_system = self.steady_state_solver.steady_state_system

        self.residuals = np.array(self.f_ss_resid(**self.steady_state_dict,
                                                  **self.free_param_dict,
                                                  **self.calib_param_dict))

        self.steady_state_solved = np.allclose(self.residuals, 0)

        if verbose:
            if self.steady_state_solved:
                print(f'Steady state found! Sum of squared residuals is {(self.residuals ** 2).sum()}')
            else:
                print(f'Steady state NOT found. Sum of squared residuals is {(self.residuals ** 2).sum()}')

    def print_steady_state(self):
        if self.steady_state_dict is None:
            print('Run the steady_state method to find a steady state before calling this method.')
            return

        if not self.steady_state_solved:
            print('Values come from the latest solver iteration but are NOT a valid steady state.')

        max_var_name = max([len(x) for x in
                            list(self.steady_state_dict.keys()) + list(self.calib_param_dict.keys())]) + 5
        for key, value in self.steady_state_dict.items():
            print(f'{key:{max_var_name}}{value:>10.3f}')

        if len(self.params_to_calibrate) > 0:
            print('\n')
            print('In addition, the following parameter values were calibrated:')
            for key, value in self.calib_param_dict.items():
                print(f'{key:{max_var_name}}{value:>10.3f}')

    def solve_model(self,
                    solver='cycle_reduction',
                    not_loglin_variable: Optional[List[str]] = None,
                    order: int = 1,
                    model_is_linear: bool = False,
                    tol: float = 1e-8,
                    max_iter: int = 1000,
                    verbose: bool = True,
                    on_failure='error') -> None:
        """
        Solve for the linear approximation to the policy function via perturbation. Adapted from R code in the gEcon
        package by Grzegorz Klima, Karol Podemski, and Kaja Retkiewicz-Wijtiwiak., http://gecon.r-forge.r-project.org/.

        Parameters
        ----------
        solver: str, default: 'cycle_reduction'
            Name of the algorithm to solve the linear solution. Currently "cycle_reduction" and "gensys" are supported.
            Following Dynare, cycle_reduction is the default, but note that gEcon uses gensys.
        not_loglin_variable: List, default: None
            Variables to not log linearize when solving the model. Variables with steady state values close to zero
            will be automatically selected to not log linearize.
        order: int, default: 1
            Order of taylor expansion to use to solve the model. Currently only 1st order approximation is supported.
        model_is_linear: bool, default: False
            Flag indicating whether a model has already been linearized by the user.
        tol: float, default 1e-8
            Desired level of floating point accuracy in the solution
        max_iter: int, default: 1000
            Maximum number of cycle_reduction iterations. Not used if solver is 'gensys'.
        verbose: bool, default: True
            Flag indicating whether to print solver results to the terminal
        on_failure: str, one of ['error', 'ignore'], default: 'error'
            Instructions on what to do if the algorithm to find a linearized policy matrix. "Error" will raise an error,
            while "ignore" will return None. "ignore" is useful when repeatedly solving the model, e.g. when sampling.

        Returns
        -------
        None
        """

        param_dict = merge_dictionaries(self.free_param_dict, self.calib_param_dict)
        steady_state_dict = self.steady_state_dict

        if self.build_perturbation_matrices is None:
            self._perturbation_setup(not_loglin_variable, order, model_is_linear, verbose, bool)

        A, B, C, D = self.build_perturbation_matrices(**param_dict, **steady_state_dict)
        _, variables, _ = self.perturbation_solver.make_all_variable_time_combinations()

        if solver == 'gensys':
            gensys_results = self.perturbation_solver.solve_policy_function_with_gensys(A, B, C, D, tol, verbose)
            G_1, constant, impact, f_mat, f_wt, y_wt, gev, eu, loose = gensys_results

            if G_1 is None:
                if on_failure == 'error':
                    raise GensysFailedException(eu)
                elif on_failure == 'ignore':
                    if verbose:
                        print(interpret_gensys_output(eu))
                    self.P = None
                    self.Q = None
                    self.R = None
                    self.S = None

                    self.perturbation_solved = False

                    return

            T = G_1[:self.n_variables, :][:, :self.n_variables]
            R = impact[:self.n_variables, :]

        elif solver == 'cycle_reduction':
            T, R, result, log_norm = self.perturbation_solver.solve_policy_function_with_cycle_reduction(A, B, C, D,
                                                                                                         max_iter,
                                                                                                         tol,
                                                                                                         verbose)
            if T is None:
                if on_failure == 'errror':
                    raise GensysFailedException(result)
        else:
            raise NotImplementedError('Only "cycle_reduction" and "gensys" are valid values for solver')

        gEcon_matrices = self.perturbation_solver.statespace_to_gEcon_representation(A, T, R, variables, tol)
        P, Q, _, _, A_prime, R_prime, S_prime = gEcon_matrices

        resid_norms = self.perturbation_solver.residual_norms(B, C, D, Q, P, A_prime, R_prime, S_prime)
        norm_deterministic, norm_stochastic = resid_norms

        if verbose:
            print(f'Norm of deterministic part: {norm_deterministic:0.9f}')
            print(f'Norm of stochastic part:    {norm_deterministic:0.9f}')

        self.T = pd.DataFrame(T, index=[x.base_name for x in sorted(self.variables, key=lambda x: x.base_name)],
                              columns=[x.base_name for x in sorted(self.variables, key=lambda x: x.base_name)])
        self.R = pd.DataFrame(R, index=[x.base_name for x in sorted(self.variables, key=lambda x: x.base_name)],
                              columns=[x.base_name for x in sorted(self.shocks, key=lambda x: x.base_name)])

        self.perturbation_solved = True

    def _perturbation_setup(self, not_loglin_variables=None,
                            order=1,
                            model_is_linear=False,
                            verbose=True,
                            return_F_matrices=False,
                            tol=1e-8):

        free_param_dict = self.free_param_dict.copy()

        parameters = list(free_param_dict.keys())
        variables = list(self.steady_state_dict.keys())
        params_to_calibrate = list(self.calib_param_dict.keys())

        params_and_variables = parameters + params_to_calibrate + variables

        shocks = self.shocks
        shock_ss_dict = dict(zip([x.to_ss() for x in shocks], np.zeros(self.n_shocks)))
        variables_and_shocks = self.variables + shocks
        valid_names = [x.base_name for x in variables_and_shocks]

        steady_state_dict = self.steady_state_dict.copy()

        # We need shocks to be zero in A, B, C, D but 1 in T; can abuse the T_dummies to accomplish that.
        if not_loglin_variables is None:
            not_loglin_variables = []

        not_loglin_variables += [x.base_name for x in shocks]

        # Validate that all user-supplied variables are in the model
        for variable in not_loglin_variables:
            if variable not in valid_names:
                raise VariableNotFoundException(variable)

        # Variables that are zero at the SS can't be log-linearized, check for these here.
        close_to_zero_warnings = []
        for variable in variables_and_shocks:
            if variable.base_name in not_loglin_variables:
                continue

            if abs(steady_state_dict[variable.to_ss().name]) < tol:
                not_loglin_variables.append(variable.base_name)
                close_to_zero_warnings.append(variable)

        if len(close_to_zero_warnings) > 0 and verbose:
            warn('The following variables have steady state values close to zero and will not be log linearized: ' +
                 ', '.join(x.base_name for x in close_to_zero_warnings))

        if order != 1:
            raise NotImplementedError

        if not self.steady_state_solved:
            raise SteadyStateNotSolvedError()

        if model_is_linear:
            warn('Model will be solved as though ALL system equations have already been linearized in the GCN file. No'
                 'checks are performed to ensure this is indeed the case. Proceed with caution.')
            Fs = self.perturbation_solver.convert_linear_system_to_matrices()

        else:
            Fs = self.perturbation_solver.log_linearize_model(not_loglin_variables=not_loglin_variables)

        Fs_subbed = [F.subs(shock_ss_dict) for F in Fs]
        self.build_perturbation_matrices = sp.lambdify(params_and_variables, Fs_subbed)

        if return_F_matrices:
            return Fs_subbed

    def check_bk_condition(self,
                           free_param_dict: Optional[Dict[str, float]] = None,
                           system_matrices: Optional[List[ArrayLike]] = None,
                           verbose: bool = True,
                           return_value: Optional[str] = 'df',
                           tol=1e-8) -> Optional[ArrayLike]:
        """
        Compute the generalized eigenvalues of system in the form presented in [1]. Per [2], the number of
        unstable eigenvalues (|v| > 1) should not be greater than the number of forward-looking variables. Failing
        this test suggests timing problems in the definition of the model.

        Parameters
        ----------
        free_param_dict: dict, optional
            A dictionary of parameter values. If None, the current stored values are used.
        verbose: bool, default: True
            Flag to print the results of the test, otherwise the eigenvalues are returned without comment.
        return_value: string, default: 'eigenvalues'
            Controls what is returned by the function. Valid values are 'df', 'flag', and None.
            If df, a dataframe containing eigenvalues is returned. If 'bool', a boolean indicating whether the BK
            condition is satisfied. If None, nothing is returned.
        tol: float, 1e-8
            Convergence tolerance for the gensys solver

        Returns
        -------

        """
        if self.build_perturbation_matrices is None:
            raise PerturbationSolutionNotFoundException()

        if free_param_dict is not None:
            ss_dict, calib_dict = self.f_ss(free_param_dict)
        else:
            free_param_dict = self.free_param_dict
            ss_dict = self.steady_state_dict
            calib_dict = self.calib_param_dict

        if system_matrices is not None:
            A, B, C, D = system_matrices
        else:
            A, B, C, D = self.build_perturbation_matrices(**ss_dict, **free_param_dict, **calib_dict)
        n_forward = (C.sum(axis=0) > 0).sum().astype(int)
        n_eq, n_vars = A.shape

        # TODO: Compute system eigenvalues -- avoids calling the whole Gensys routine, but there is code duplication
        #   building Gamma_0 and Gamma_1
        lead_var_idx = np.where(np.sum(np.abs(C), axis=0) > tol)[0]

        eqs_and_leads_idx = np.r_[np.arange(n_vars), lead_var_idx + n_vars].tolist()

        Gamma_0 = np.vstack([np.hstack([B, C]),
                             np.hstack([-np.eye(n_eq), np.zeros((n_eq, n_eq))])])

        Gamma_1 = np.vstack([np.hstack([A, np.zeros((n_eq, n_eq))]),
                             np.hstack([np.zeros((n_eq, n_eq)), np.eye(n_eq)])])
        Gamma_0 = Gamma_0[eqs_and_leads_idx, :][:, eqs_and_leads_idx]
        Gamma_1 = Gamma_1[eqs_and_leads_idx, :][:, eqs_and_leads_idx]

        # A, B, Q, Z = qzdiv(1.01, *linalg.qz(-Gamma_0, Gamma_1, 'complex'))

        # Using scipy instead of qzdiv appears to offer a huge speedup for nearly the same answer; some eigenvalues
        # have sign flip relative to qzdiv -- does it matter?
        A, B, alpha, beta, Q, Z = linalg.ordqz(-Gamma_0, Gamma_1, sort='ouc', output='complex')

        gev = np.c_[np.diagonal(A), np.diagonal(B)]

        eigenval = gev[:, 1] / (gev[:, 0] + tol)
        pos_idx = np.where(np.abs(eigenval) > 0)
        eig = np.zeros(((np.abs(eigenval) > 0).sum(), 3))
        eig[:, 0] = np.abs(eigenval)[pos_idx]
        eig[:, 1] = np.real(eigenval)[pos_idx]
        eig[:, 2] = np.imag(eigenval)[pos_idx]

        sorted_idx = np.argsort(eig[:, 0])
        eig = pd.DataFrame(eig[sorted_idx, :], columns=['Modulus', 'Real', 'Imaginary'])

        n_g_one = (eig['Modulus'] > 1).sum()
        condition_not_satisfied = n_forward > n_g_one
        if verbose:
            print(f'Model solution has {n_g_one} eigenvalues greater than one in modulus and {n_forward} '
                  f'forward-looking variables.'
                  f'\nBlanchard-Kahn condition is{" NOT" if condition_not_satisfied else ""} satisfied.')

        if return_value is None:
            return

        if return_value == 'df':
            return eig
        elif return_value == 'bool':
            return ~condition_not_satisfied

    def compute_stationary_covariance_matrix(self):
        """
        Compute the stationary covariance matrix of the solved system via fixed-point iteration. By construction, any
        linearized DSGE model will have a fixed covariance matrix. In principal, a closed form solution is available
        (we could solve a discrete Lyapunov equation) but this works fine.

        Returns
        -------
        sigma: DataFrame
        """
        if not self.perturbation_solved:
            raise PerturbationSolutionNotFoundException()

        T, R = self.T, self.R

        # TODO: Should this be R @ Q @ R.T ?
        sigma = linalg.solve_discrete_lyapunov(T.values, R.values @ R.values.T)

        return pd.DataFrame(sigma / 100, index=T.index, columns=T.index)

    def compute_autocorrelation_matrix(self, n_lags=10):
        """
        Computes autocorrelations for each model variable using the stationary covariance matrix. See doc string for
        compute_stationary_covariance_matrix for more information.

        Parameters
        ----------
        n_lags: int
            Number of lags over which to compute the autocorrelation

        Returns
        -------
        acorr_mat: DataFrame
        """
        if not self.perturbation_solved:
            raise PerturbationSolutionNotFoundException()

        T, R = self.T, self.R

        Sigma = linalg.solve_discrete_lyapunov(T.values, R.values @ R.values.T)
        acorr_mat = _compute_autocorrelation_matrix(T.values, Sigma, n_lags=n_lags)

        return pd.DataFrame(acorr_mat, index=T.index, columns=np.arange(n_lags))

    def sample_param_dict_from_prior(self, n_samples=1, seed=None, param_subset=None):
        n_params = len(self.param_priors)
        if seed is not None:
            seed_sequence = np.random.SeedSequence(seed)
            child_seeds = seed_sequence.spawn(n_params)
            streams = [np.random.default_rng(s) for s in child_seeds]
        else:
            streams = [None] * n_params

        new_param_dict = self.free_param_dict.copy()
        if param_subset is None:
            priors_to_sample = self.param_priors
        else:
            priors_to_sample = {k: v for k, v in self.param_priors.items() if k in param_subset}

        for i, (key, d) in enumerate(priors_to_sample.items()):
            new_param_dict[key] = d.rvs(size=n_samples, random_state=streams[i])

        return new_param_dict

    def impulse_response_function(self, simulation_length: int = 40, shock_size: float = 1.0):
        if not self.perturbation_solved:
            raise PerturbationSolutionNotFoundException()

        T, R = self.T, self.R

        timesteps = simulation_length

        data = np.zeros((self.n_variables, timesteps, self.n_shocks))

        for i in range(self.n_shocks):
            shock_path = np.zeros((self.n_shocks, timesteps))
            shock_path[i, 0] = shock_size

            for t in range(1, timesteps):
                stochastic = R.values @ shock_path[:, t - 1]
                deterministic = T.values @ data[:, t - 1, i]
                data[:, t, i] = (deterministic + stochastic)

        index = pd.MultiIndex.from_product([R.index,
                                            np.arange(timesteps),
                                            R.columns],
                                           names=['Variables', 'Time', 'Shocks'])

        df = (pd.DataFrame(data.ravel(), index=index, columns=['Values'])
              .unstack([1, 2])
              .droplevel(axis=1, level=0)
              .sort_index(axis=1))

        return df

    def simulate(self, simulation_length: int = 40,
                 n_simulations: int = 100,
                 shock_dict: Optional[Dict[str, float]] = None,
                 shock_cov_matrix: Optional[ArrayLike] = None,
                 show_progress_bar: bool = False):

        if not self.perturbation_solved:
            raise PerturbationSolutionNotFoundException()

        T, R = self.T, self.R
        timesteps = simulation_length

        n_shocks = R.shape[1]

        if shock_cov_matrix is not None:
            assert shock_cov_matrix.shape == (
                n_shocks, n_shocks), f'The shock covariance matrix should have shape {n_shocks} x {n_shocks}'
            d = stats.multivariate_normal(mean=np.zeros(n_shocks), cov=shock_cov_matrix)
            epsilons = np.r_[[d.rvs(timesteps) for _ in range(n_simulations)]]

        elif shock_dict is not None:
            epsilons = np.zeros((n_simulations, timesteps, n_shocks))
            for i, shock in enumerate(self.shocks):
                if shock.base_name in shock_dict.keys():
                    d = stats.norm(loc=0, scale=shock_dict[shock.base_name])
                    epsilons[:, :, i] = np.r_[[d.rvs(timesteps) for _ in range(n_simulations)]]

        elif all([shock.base_name in self.shock_priors.keys() for shock in self.shocks]):
            epsilons = np.zeros((n_simulations, timesteps, n_shocks))
            for i, d in enumerate(self.shock_priors.values()):
                epsilons[:, :, i] = np.r_[[d.rvs(timesteps) for _ in range(n_simulations)]]

        else:
            raise ValueError('To run a simulation, supply either a full covariance matrix, a dictionary of shocks and'
                             'standard deviations, or specify priors on the shocks in your GCN file.')

        data = np.zeros((self.n_variables, timesteps, n_simulations))
        if epsilons.ndim == 2:
            epsilons = epsilons[:, :, None]

        progress_bar = ProgressBar(timesteps - 1, verb='Sampling')

        for t in range(1, timesteps):
            progress_bar.start()
            stochastic = np.einsum('ij,sj', R.values, epsilons[:, t - 1, :])
            deterministic = T.values @ data[:, t - 1, :]
            data[:, t, :] = (deterministic + stochastic)

            if show_progress_bar:
                progress_bar.stop()

        index = pd.MultiIndex.from_product([R.index,
                                            np.arange(timesteps),
                                            np.arange(n_simulations)],
                                           names=['Variables', 'Time', 'Simulation'])
        df = pd.DataFrame(data.ravel(), index=index, columns=['Values']).unstack([1, 2]).droplevel(axis=1, level=0)

        return df

    def _build_prior_dict(self, prior_dict: Dict[str, str], build_pymc: bool) -> None:
        """
        Parameters
        ----------
        prior_dict: dict
            Dictionary of variable_name: distribution_string pairs, prepared by the parse_gcn function.
        build_pymc: str
            If True, build a PyMC model with declared random variables.

        Returns
        -------
        self.param_dict: dict
            Dictionary of variable:distribution pairs. Distributions are scipy rv_frozen objects, unless the
            distribution is parameterized by another distribution, in which case a "CompositeDistribution" object
            with methods .rvs, .pdf, and .logpdf is returned.
        """

        priors = create_prior_distribution_dictionary(prior_dict)
        if build_pymc:
            self.pymc_model = build_pymc_model(prior_dict)

        hyper_parameters = set(prior_dict.keys()) - set(priors.keys())

        # Clean up the hyper-parameters (e.g. shock stds) from the model, they aren't needed anymore
        for parameter in hyper_parameters:
            del self.free_param_dict[parameter]

        param_priors = {}
        shock_priors = {}
        for key, value in priors.items():
            sympy_key = single_symbol_to_sympy(key)
            if isinstance(sympy_key, TimeAwareSymbol):
                shock_priors[sympy_key.base_name] = value
            else:
                param_priors[sympy_key.name] = value

        self.param_priors = param_priors
        self.shock_priors = shock_priors

    def _build_model_blocks(self, parsed_model):
        raw_blocks = gEcon_parser.split_gcn_into_block_dictionary(parsed_model)

        self.options = raw_blocks['options']
        self.try_reduce_vars = raw_blocks['tryreduce']

        del raw_blocks['options']
        del raw_blocks['tryreduce']

        self._get_steady_state_equations(raw_blocks)

        for block_name, block_content in raw_blocks.items():
            block_dict = gEcon_parser.parsed_block_to_dict(block_content)
            block = Block(name=block_name, block_dict=block_dict)
            block.solve_optimization()

            self.blocks[block.name] = block

        self.n_blocks = len(self.blocks)

    def _get_all_block_equations(self) -> None:
        _, blocks = unpack_keys_and_values(self.blocks)
        for block in blocks:
            self.system_equations.extend(block.system_equations)
        self.n_equations = len(self.system_equations)

    def _get_all_block_parameters(self) -> None:
        _, blocks = unpack_keys_and_values(self.blocks)
        for block in blocks:
            self.free_param_dict.update(block.param_dict)

        self.free_param_dict = sequential(self.free_param_dict,
                                          [sympy_keys_to_strings, sympy_number_values_to_floats, sort_dictionary])

    def _get_all_block_params_to_calibrate(self) -> None:
        _, blocks = unpack_keys_and_values(self.blocks)
        for block in blocks:
            if block.params_to_calibrate is None:
                continue

            if len(self.params_to_calibrate) == 0:
                self.params_to_calibrate = block.params_to_calibrate
            else:
                self.params_to_calibrate.extend(block.params_to_calibrate)

            if block.calibrating_equations is None:
                continue

            if len(self.calibrating_equations) == 0:
                self.calibrating_equations = block.calibrating_equations
            else:
                self.calibrating_equations.extend(block.calibrating_equations)

        self.n_calibrating_equations = len(self.calibrating_equations)
        self.n_params_to_calibrate = len(self.params_to_calibrate)

    def _get_variables_and_shocks(self) -> None:
        all_shocks = []
        _, blocks = unpack_keys_and_values(self.blocks)

        for block in blocks:
            if block.shocks is not None:
                all_shocks.extend([x for x in block.shocks])
        self.shocks = all_shocks
        self.n_shocks = len(all_shocks)

        for eq in self.system_equations:
            atoms = eq.atoms()
            variables = [x for x in atoms if is_variable(x)]
            for variable in variables:
                if variable.set_t(0) not in self.variables and variable not in all_shocks:
                    self.variables.append(variable.set_t(0))
        self.n_variables = len(self.variables)

        self.variables = sorted(self.variables, key=lambda x: x.name)
        self.shocks = sorted(self.shocks, key=lambda x: x.name)

    def _get_steady_state_equations(self, raw_blocks: Dict[str, List[str]]):
        block_names = raw_blocks.keys()
        ss_block_names = [name for name in block_names if name in STEADY_STATE_NAMES]
        n_ss_blocks = len(ss_block_names)

        if n_ss_blocks == 0:
            return
        if n_ss_blocks > 1:
            raise MultipleSteadyStateBlocksException(ss_block_names)

        block_content = raw_blocks[ss_block_names[0]]
        block_dict = gEcon_parser.parsed_block_to_dict(block_content)
        block = Block(name='steady_state', block_dict=block_dict)

        sub_dict = dict()
        steady_state_dict = dict()

        if block.definitions is not None:
            _, definitions = unpack_keys_and_values(block.definitions)
            sub_dict = {eq.lhs: eq.rhs for eq in definitions}

        if block.identities is not None:
            _, identities = unpack_keys_and_values(block.identities)
            for eq in identities:
                subbed_rhs = eq.rhs.subs(sub_dict)
                steady_state_dict[eq.lhs] = subbed_rhs
                sub_dict[eq.lhs] = subbed_rhs

        self.steady_state_relationships = sequential(steady_state_dict,
                                                     [sympy_keys_to_strings, sympy_number_values_to_floats,
                                                      sort_dictionary])

        del raw_blocks[ss_block_names[0]]

    def _try_reduce(self):
        if self.try_reduce_vars is None:
            return

        self.try_reduce_vars = [single_symbol_to_sympy(x) for x in self.try_reduce_vars]

        variables = self.variables
        n_variables = self.n_variables

        occurrence_matrix = np.zeros((n_variables, n_variables))
        reduced_system = []

        for i, eq in enumerate(self.system_equations):
            for j, var in enumerate(self.variables):
                if any([x in eq.atoms() for x in make_all_var_time_combos([var])]):
                    occurrence_matrix[i, j] += 1

        # Columns with a sum of 1 are variables that appear only in a single equations; these equations can be deleted
        # without consequence w.r.t solving the system.

        isolated_variables = np.array(variables)[occurrence_matrix.sum(axis=0) == 1]
        to_remove = set(isolated_variables).intersection(set(self.try_reduce_vars))

        for eq in self.system_equations:
            if not any([var in eq.atoms() for var in to_remove]):
                reduced_system.append(eq)

        self.system_equations = reduced_system
        self.n_equations = len(self.system_equations)

        self.variables = set(atom.set_t(0) for eq in reduced_system for atom in eq.atoms() if is_variable(atom))
        self.variables -= set(self.shocks)
        self.variables = sorted(list(self.variables), key=lambda x: x.name)
        self.n_variables = len(self.variables)

        if self.n_equations != self.n_variables:
            raise ValueError

        eliminated_vars = [var.name for var in variables if var not in self.variables]

        return eliminated_vars

    def _simplify_singletons(self):
        system = self.system_equations

        variables = self.variables
        reduce_dict = {}

        for eq in system:
            if len(eq.atoms()) < 4:
                var = [x for x in eq.atoms() if is_variable(x)]
                if len(var) > 1:
                    continue
                var = var[0]
                sub_dict = expand_subs_for_all_times(sp.solve(eq, var, dict=True)[0])
                reduce_dict.update(sub_dict)

        reduced_system = substitute_all_equations(system, reduce_dict)
        reduced_system = [eq for eq in reduced_system if eq != 0]

        self.system_equations = reduced_system
        self.n_equations = len(reduced_system)

        self.variables = set(atom.set_t(0) for eq in reduced_system for atom in eq.atoms() if is_variable(atom))
        self.variables -= set(self.shocks)
        self.variables = sorted(list(self.variables), key=lambda x: x.name)
        self.n_variables = len(self.variables)

        if self.n_equations != self.n_variables:
            raise ValueError

        eliminated_vars = [var.name for var in variables if var not in self.variables]

        return eliminated_vars


# @njit
# def _compute_stationary_covariance_matrix(A, C, tol=1e-9, max_iter=10_000):
#     sigma = np.eye(A.shape[0])
#     for _ in range(max_iter):
#         new_sigma = A @ sigma @ A.T + C @ C.T
#         if ((sigma - new_sigma) ** 2).mean() < tol:
#             return sigma
#         else:
#             sigma = new_sigma

@njit
def _compute_autocorrelation_matrix(A, sigma, n_lags=5):
    acov = np.zeros((A.shape[0], n_lags))
    acov_factor = np.eye(A.shape[0])
    for i in range(n_lags):
        cov = acov_factor @ sigma
        acov[:, i] = np.diag(cov) / np.diag(sigma)
        acov_factor = A @ acov_factor

    return acov
