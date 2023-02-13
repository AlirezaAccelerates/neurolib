import abc
import numba
import numpy as np
from neurolib.optimal_control import cost_functions
import logging
import copy
from scipy.signal import hilbert


def getdefaultweights():
    weights = numba.typed.Dict.empty(
        key_type=numba.types.unicode_type,
        value_type=numba.types.float64,
    )
    weights["w_p"] = 1.0
    weights["w_f"] = 0.0
    weights["w_phase"] = 0.0
    weights["w_ac"] = 0.0
    weights["w_2"] = 0.0
    weights["w_1"] = 0.0
    weights["w_1T"] = 0.0
    weights["w_1D"] = 0.0

    return weights


def decrease_step(controlled_model, cost, cost0, step, control0, factor_down, cost_gradient):
    """Find a step size which leads to improved cost given the gradient. The step size is iteratively decreased.
        The control-inputs are updated in place according to the found step size via the
        "controlled_model.update_input()" call.

    :param controlled_model: Instance of optimal control object.
    :type controlled_model:  neurolib.optimal_control.oc.OC
    :param cost:    Cost after applying control update according to gradient with first valid step size (numerically
                    stable).
    :type cost:     float
    :param cost0:   Cost without updating the control.
    :type cost0:    float
    :param step:    Step size initial to the iterative decreasing.
    :type step:     float
    :param control0:    The unchanged control signal.
    :type control0:     np.ndarray N x V x T
    :param factor_down:  Factor the step size is scaled with in each iteration until cost is improved.
    :type factor_down:   float
    :param cost_gradient:   Gradient of the total cost wrt. to the control signal.
    :type cost_gradient:    np.ndarray of shape N x V x T
    :return:    The selected step size and the count-variable how often step-adjustment-loop was executed.
    :rtype:     tuple[float, int]
    """
    if controlled_model.M > 1:
        noisy = True
    else:
        noisy = False

    counter = 0

    while cost > cost0:  # Decrease the step size until first step size is found where cost is improved.
        step *= factor_down  # Decrease step size.
        counter += 1

        # Inplace updating of models control bc. forward-sim relies on models parameters.
        controlled_model.control = update_control_with_limit(
            control0, step, cost_gradient, controlled_model.maximum_control_strength
        )
        controlled_model.update_input()

        # Simulate with control updated according to new step and evaluate cost.
        controlled_model.simulate_forward()

        if noisy:
            cost = controlled_model.compute_cost_noisy(controlled_model.M)
        else:
            cost = controlled_model.compute_total_cost()

        if counter == controlled_model.count_step:  # Exit if the maximum search depth is reached without improvement of
            # cost.
            step = 0.0  # For later analysis only.
            controlled_model.control = update_control_with_limit(
                control0, 0.0, np.zeros(control0.shape), controlled_model.maximum_control_strength
            )
            controlled_model.update_input()

            controlled_model.zero_step_encountered = True
            break

    return step, counter


def increase_step(controlled_model, cost, cost0, step, control0, factor_up, cost_gradient):
    """Find the largest step size which leads to the biggest improvement of cost given the gradient. The step size is
        iteratively increased.
        The control-inputs are updated in place according to the found step size via the
        "controlled_model.update_input()" call.

    :param controlled_model: Instance of optimal control object.
    :type controlled_model:  neurolib.optimal_control.oc.OC
    :param cost:    Cost after applying control update according to gradient with first valid step size (numerically
                    stable).
    :type cost:     float
    :param cost0:   Cost without updating the control.
    :type cost0:    float
    :param step:    Step size initial to the iterative decreasing.
    :type step:     float
    :param control0:    The unchanged control signal.
    :type control0:     np.ndarray N x V x T
    :param factor_up:  Factor the step size is scaled with in each iteration while the cost keeps improving.
    :type factor_up:   float
    :param cost_gradient:   Gradient of the total cost wrt. to the control signal.
    :type cost_gradient:    np.ndarray of shape N x V x T
    :return:    The selected step size and the count-variable how often step-adjustment-loop was executed.
    :rtype:     tuple[float, int]
    """
    if controlled_model.M > 1:
        noisy = True
    else:
        noisy = False

    cost_prev = cost0
    counter = 0

    while cost < cost_prev:  # Increase the step size as long as the cost is improving.
        step *= factor_up
        counter += 1

        # Inplace updating of models control bc. forward-sim relies on models parameters
        controlled_model.control = update_control_with_limit(
            control0, step, cost_gradient, controlled_model.maximum_control_strength
        )
        controlled_model.update_input()

        controlled_model.simulate_forward()
        if np.isnan(
            controlled_model.get_xs()
        ).any():  # Go back to last step (that was numerically stable and improved cost)
            # and exit.
            logging.info("Increasing step encountered NAN.")
            step /= factor_up  # Undo the last step update by inverse operation.
            controlled_model.control = update_control_with_limit(
                control0, step, cost_gradient, controlled_model.maximum_control_strength
            )
            controlled_model.update_input()
            break

        else:

            if noisy:
                cost = controlled_model.compute_cost_noisy(controlled_model.M)
            else:
                cost = controlled_model.compute_total_cost()

            if cost > cost_prev:  # If the cost increases: go back to last step (that resulted in best cost until
                # then) and exit.
                step /= factor_up  # Undo the last step update by inverse operation.
                controlled_model.control = update_control_with_limit(
                    control0, step, cost_gradient, controlled_model.maximum_control_strength
                )
                controlled_model.update_input()
                break

            else:
                cost_prev = cost  # Memorize cost with this step size for comparison in next step-update.

            if counter == controlled_model.count_step:
                # Terminate step size search at count limit, exit with the highest found, valid and best performing step
                # size.
                break

    return step, counter


@numba.njit
def solve_adjoint(hx, hx_nw, fx, state_dim, dt, N, T):
    """Backwards integration of the adjoint state.

    :param hx: dh/dx    Jacobians of systems dynamics wrt. to 'state_vars' for each time step.
    :type hx:           np.ndarray
    :param hx_nw:       Jacobians for each time step for the network coupling.
    :type hx_nw:        np.ndarray
    :param fx: df/dx    Derivative of cost function wrt. to systems dynamics.
    :type fx:           np.ndarray
    :param state_dim:   Dimensions of state (N, V, T).
    :type state_dim:    tuple
    :param dt:          Time resolution of integration.
    :type dt:           float
    :param N:           Number of nodes in the network.
    :type N:            int
    :param T:           Length of simulation (time dimension).
    :type T:            int
    :return:            Adjoint state.
    :rtype:             np.ndarray of shape `state_dim`
    """
    # ToDo: generalize, not only precision cost
    adjoint_state = np.zeros(state_dim)
    fx_fullstate = np.zeros(state_dim)
    fx_fullstate[:, :2, :] = fx.copy()

    for t in range(T - 2, -1, -1):
        for n in range(N):
            der = fx_fullstate[n, :, t + 1].copy()
            for k in range(len(der)):
                for i in range(len(der)):
                    der[k] += adjoint_state[n, i, t + 1] * hx[n, t + 1][i, k]
            for n2 in range(N):
                for k in range(len(der)):
                    for i in range(len(der)):
                        der[k] += adjoint_state[n2, i, t + 1] * hx_nw[n2, n, t + 1][i, k]
            adjoint_state[n, :, t] = adjoint_state[n, :, t + 1] - der * dt

    return adjoint_state


# @numba.njit
def compute_gradient(N, dim_out, T, df_du, adjoint_state, control_matrix, d_du):
    """Compute the gradient of the total cost wrt. to the control signals (explicitly and implicitly) given the adjoint
       state, the Jacobian of the total cost wrt. to explicit control contributions and the Jacobian of the dynamics
       wrt. to explicit control contributions.

    :param N:       Number of nodes in the network.
    :type N:        int
    :param dim_out: Number of 'output variables' of the model.
    :type dim_out:  int
    :param T:       Length of simulation (time dimension).
    :type T:        int
    :param df_du:      Derivative of the cost wrt. to the explicit control contributions to cost functionals.
    :type df_du:       np.ndarray of shape N x V x T
    :param adjoint_state:   Solution of the adjoint equation.
    :type adjoint_state:    np.ndarray of shape N x V x T
    :param control_matrix:  Binary matrix that defines nodes and variables where control inputs are active, defaults to
                            None.
    :type control_matrix:   np.ndarray of shape N x V
    :param d_du:    Jacobian of systems dynamics wrt. to I_ext (external control input)
    :type d_du:     np.ndarray of shape V x V
    :return:        The gradient of the total cost wrt. to the control.
    :rtype:         np.ndarray of shape N x V x T
    """
    grad = np.zeros(df_du.shape)

    # if derivative of dynamics wrt control is constant
    if len(d_du.shape) == 2:
        for n in range(N):
            for v in range(dim_out):
                for t in range(T):
                    grad[n, v, t] = df_du[n, v, t] + adjoint_state[n, v, t] * control_matrix[n, v] * d_du[v, v]

    # else if derivative of dynamics wrt control is time-dependent
    elif len(d_du.shape) == 4:
        for n in range(N):
            for v in range(dim_out):
                for t in range(T):
                    grad[n, v, t] = df_du[n, v, t] + adjoint_state[n, v, t] * control_matrix[n, v] * d_du[n, v, v, t]

    return grad


@numba.njit
def update_control_with_limit(control, step, gradient, u_max):
    """Computes the updated control signal. The absolute values of the new control are bounded by +/- 'u_max'. If
       'u_max' is 'None', no limit is applied.

    :param control:         N x V x T array. Control signals.
    :type control:          np.ndarray
    :param step:            Step size along the gradients.
    :type step:             float
    :param gradient:        N x V x T array of the gradients.
    :type gradient:         np.ndarray
    :param u_max:           Maximum absolute value allowed for the strength of the control signal.
    :type u_max:            float or None
    :return:                N x V x T array containing the new control signal, updated according to 'step' and
                            'gradient' with the maximum absolute values being limited by 'u_max'.
    :rtype:                 np.ndarray
    """

    control_new = control + step * gradient

    if u_max is not None:

        control_new = control + step * gradient

        for n in range(control_new.shape[0]):
            for v in range(control_new.shape[1]):
                for t in range(control_new.shape[2]):
                    if np.greater(np.abs(control_new[n, v, t]), u_max):
                        control_new[n, v, t] = np.sign(control_new[n, v, t]) * u_max

    return control_new


def convert_interval(interval, array_length):
    """Turn indices into positive values only. It is assumed in any case, that the first index defines the start and
       the second the stop index, both inclusive.

    :param interval:    Tuple containing start and stop index. May contain negative indices or 'None'.
    :type interval:     tuple
    :param array_length:    Length of the array in the dimension, along which the 'interval' is defining the slice.
    :type array_length:     int
    :return:            Tuple containing two positive 'int' indicating the start- and stop-index (both inclusive) of the
                        interval.
    :rtype:             tuple
    """

    (interval_0, interval_1) = interval

    if interval_0 is None:
        interval_0_new = 0
    elif interval_0 < 0:
        assert interval_0 > -array_length, "Interval is not specified in valid range."
        interval_0_new = array_length + interval_0  # interval entry is negative
    else:
        interval_0_new = interval_0

    if interval_1 is None:
        interval_1_new = array_length
    elif interval_1 < 0:
        assert interval_1 > -array_length, "Interval is not specified in valid range."
        interval_1_new = array_length + interval_1  # interval entry is negative
    else:
        interval_1_new = interval_1

    assert interval_0_new < interval_1_new, "Order of indices for interval is not valid."
    assert interval_1_new <= array_length, "Interval is not specified in valid range."

    return interval_0_new, interval_1_new


class OC:
    def __init__(
        self,
        model,
        target,
        weights=None,
        maximum_control_strength=None,
        print_array=[],
        cost_interval=(None, None),
        cost_matrix=None,
        control_matrix=None,
        M=1,
        M_validation=0,
        validate_per_step=False,
    ):
        """
        Base class for optimal control. Model specific methods should be implemented in derived class for each model.

        :param model:       An instance of neurolib's Model-class. Parameters like '.duration' and methods like '.run()'
                            are used within the optimal control.
        :type model:        neurolib.models.model
        :param target:      Target time series of controllable variables.
        :type target:       np.ndarray
        :param weights:     Dictionary of weight parameters, defaults to 'None'.
        :type weights:      dictionary, optional
        :param maximum_control_strength:    Maximum absolute value a control signal can take. No limitation of the
                                            absolute control strength if 'None'. Defaults to None.
        :type:                              float or None, optional
        :param print_array: Array of optimization-iteration-indices (starting at 1) in which cost is printed out.
                            Defaults to empty list `[]`.
        :type print_array:  list, optional
        :param cost_interval: (t_start, t_end). Indices of start and end point (both inclusive) of the
                                        time interval in which the precision cost is evaluated. Default is full time
                                        series. Defaults to (None, None).
        :type cost_interval:  tuple, optional
        :param cost_matrix: N x V binary matrix that defines nodes and channels of precision measurement, defaults
                                 to None.
        :type cost_matrix:  np.ndarray
        :param control_matrix:   N x V Binary matrix that defines nodes and variables where control inputs are active,
                                 defaults to None.
        :type control_matrix:    np.ndarray
        :param M:                   Number of noise realizations. M=1 implies deterministic case. Defaults to 1.
        :type M:                    int, optional
        :param M_validation:        Number of noise realizations for validation (only used in stochastic case, M>1).
                                    Defaults to 0.
        :type M_validation:         int, optional
        :param validate_per_step:   True for validation in each iteration of the optimization, False for
                                    validation only after final optimization iteration (only used in stochastic case,
                                    M>1). Defaults to False.
        :type validate_per_step:    bool, optional

        """

        self.model = copy.deepcopy(model)

        self.dt = self.model.params["dt"]  # maybe redundant but for now code clarity
        self.duration = self.model.params["duration"]  # maybe redundant but for now code clarity

        self.T = np.around(self.duration / self.dt, 0).astype(int) + 1  # Total number of time steps is
        self.N = self.model.params.N

        self.dim_vars = len(self.model.state_vars)
        self.dim_out = len(self.model.output_vars)

        # + forward simulation steps of neurolibs model.run().
        self.state_dim = (
            self.N,
            self.dim_vars,
            self.T,
        )  # dimensions of state. Model has N network nodes, V state variables, T time points

        if self.model.getMaxDelay() > 0.0:
            print("Delay not yet implemented, please set delays to zero")

        if isinstance(target, int):
            target = float(target)

        if isinstance(target, np.ndarray):
            print("Optimal control with target time series")
            self.target_timeseries = target
            self.target_period = 0.0
        elif isinstance(target, float):
            print("Optimal control with target oscillation period")
            self.target_timeseries = np.zeros((self.N, self.dim_out, self.T))
            self.target_period = target

        self.maximum_control_strength = maximum_control_strength

        if weights is None:
            weights = getdefaultweights()
        self.weights = weights

        if self.N > 1:  # check that coupling matrix has zero diagonal
            assert np.all(np.diag(self.model.Cmat) == 0.0)
        elif self.N == 1:
            if isinstance(self.model.Cmat, type(None)):
                self.model.Cmat = np.zeros((self.N, self.N))
            if isinstance(self.model.Dmat, type(None)):
                self.model.Dmat = np.zeros((self.N, self.N))

        self.Dmat_ndt = np.around(self.model.Dmat / self.model.params.dt).astype(int)

        self.cost_matrix = cost_matrix
        if isinstance(self.cost_matrix, type(None)):
            self.cost_matrix = np.ones(
                (self.N, self.dim_out)
            )  # default: measure precision in all variables in all nodes

        # check if matrix is binary
        assert np.array_equal(self.cost_matrix, self.cost_matrix.astype(bool))

        self.control_matrix = control_matrix
        if isinstance(self.control_matrix, type(None)):
            self.control_matrix = np.ones((self.N, self.dim_vars))  # default: all channels in all nodes active

        # check if matrix is binary
        assert np.array_equal(self.control_matrix, self.control_matrix.astype(bool))

        self.M = max(1, M)
        self.M_validation = M_validation

        self.step = 10.0  # Initial step size in first optimization iteration.
        self.count_noisy_step = 10
        self.count_step = 20

        self.factor_down = 0.5  # Factor for adaptive step size reduction.
        self.factor_up = 2.0  # Factor for adaptive step size increment.

        self.cost_validation = 0.0
        self.validate_per_step = validate_per_step

        if self.model.params.sigma_ou != 0.0:  # noisy system
            if self.M <= 1:
                logging.warning(
                    "For noisy system, please chose parameter M larger than 1 (recommendation > 10)."
                    + "\n"
                    + 'If you want to study a deterministic system, please set model parameter "sigma_ou" to zero'
                )
            if self.M > self.M_validation:
                print('Parameter "M_validation" should be chosen larger than parameter "M".')
        else:  # deterministic system
            if self.M > 1 or self.M_validation != 0 or validate_per_step:
                print(
                    'For deterministic systems, parameters "M", "M_validation" and "validate_per_step" are not relevant.'
                    + "\n"
                    + 'If you want to study a noisy system, please set model parameter "sigma_ou" larger than zero'
                )

        self.adjoint_state = np.zeros(self.state_dim)

        self.control = None  # Is implemented in derived classes.

        self.cost_history = []
        self.step_sizes_history = []
        self.step_sizes_loops_history = []

        # save control signals throughout optimization iterations for later analysis
        self.control_history = []

        self.print_array = print_array

        self.zero_step_encountered = False  # deterministic gradient descent cannot further improve

        self.cost_interval = convert_interval(cost_interval, self.T)

    @abc.abstractmethod
    def get_xs(self):
        """Stack the initial condition with the simulation results for controllable state variables."""
        pass

    def simulate_forward(self):
        """Updates 'state_vars' of 'self.model' in accordance to the current 'self.control'. Results for the controllable state
        variables can be accessed with self.get_xs()
        """
        self.model.run()

    @abc.abstractmethod
    def update_input(self):
        """Update the parameters in 'self.model' according to the current control such that self.simulate_forward
        operates with the appropriate control signal.
        """
        pass

    @abc.abstractmethod
    def Dxdot(self):
        """V x V Jacobian of systems dynamics wrt. to change of all 'state_vars'."""
        pass

    @abc.abstractmethod
    def Duh(self):
        """Jacobian of systems dynamics wrt. to external inputs (control signals) to all 'state_vars'."""
        pass

    def compute_total_cost(self):
        """Compute the total cost as weighted sum precision of precision and L2 term.

        :rtype: float
        """
        xs = self.get_xs()
        accuracy_cost = cost_functions.accuracy_cost(
            xs,
            hilbert(xs),
            self.target_timeseries,
            self.target_period,
            self.weights,
            self.cost_matrix,
            self.dt,
            self.cost_interval,
        )
        control_strenght_cost = cost_functions.control_strength_cost(self.control, self.weights, self.dt)
        return (
            accuracy_cost + control_strenght_cost
        )  # Further cost terms can be added here. Add corresponding derivatives
        # elsewhere accordingly.

    def compute_gradient(self):
        """Compute the gradient of the total cost wrt. to the control signals. This is achieved by first, solving the
        adjoint equation backwards in time. Second, derivatives of the cost wrt. to explicit control variables are
        evaluated as well as the Jacobians of the dynamics wrt. to explicit control. Then the decent direction /
        gradient of the cost wrt. to control (in its explicit form AND IMPLICIT FORM) is computed.
        :return:        The gradient of the total cost wrt. to the control.
        :rtype:         np.ndarray of shape N x V x T
        """
        self.solve_adjoint()
        df_du = cost_functions.derivative_control_strength_cost(self.control, self.weights, self.dt)
        duh = self.Duh()

        return compute_gradient(self.N, self.dim_out, self.T, df_du, self.adjoint_state, self.control_matrix, duh)

    @abc.abstractmethod
    def compute_hx(self):
        """Jacobians of model dynamics wrt. to its 'state_vars' at each time step."""
        pass

    @abc.abstractmethod
    def compute_hx_nw(self):
        """Jacobians for each time step for the network coupling."""
        pass

    def solve_adjoint(self):
        """Backwards integration of the adjoint state."""

        hx = self.compute_hx()
        hx_nw = self.compute_hx_nw()
        xs = self.get_xs()

        # Derivative of cost wrt. to controllable 'state_vars'. Contributions of other costs might be added here.
        df_dx = cost_functions.derivative_accuracy_cost(
            xs,
            hilbert(xs),
            self.target_timeseries,
            self.target_period,
            self.weights,
            self.cost_matrix,
            self.dt,
            self.cost_interval,
        )

        self.adjoint_state = solve_adjoint(hx, hx_nw, df_dx, self.state_dim, self.dt, self.N, self.T)

    def decrease_step(self, cost, cost0, step, control0, factor_down, cost_gradient):
        """Iteratively decrease step size until cost is improved."""
        return decrease_step(self, cost, cost0, step, control0, factor_down, cost_gradient)

    def increase_step(self, cost, cost0, step, control0, factor_up, cost_gradient):
        """Iteratively increase step size while cost is improving."""
        return increase_step(self, cost, cost0, step, control0, factor_up, cost_gradient)

    def step_size(self, cost_gradient):
        """Adaptively choose a step size for control update.

        :param cost_gradient:   N x V x T gradient of the total cost wrt. to control.
        :type cost_gradient:    np.ndarray
        :return:    Step size that got multiplied with the 'cost_gradient'.
        :rtype:     float
        """
        if self.M > 1:
            noisy = True
        else:
            noisy = False

        self.simulate_forward()
        if noisy:
            cost0 = self.compute_cost_noisy(self.M)
        else:
            cost0 = (
                self.compute_total_cost()
            )  # Current cost without updating the control according to the "cost_gradient".

        step = self.step  # Load step size of last optimization-iteration as initial guess.

        control0 = self.control  # Memorize unchanged control throughout step-size computation.

        while True:  # Reduce the step size, if numerical instability occurs in the forward-simulation.
            # inplace updating of models control bc. forward-sim relies on models parameters
            self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
            self.update_input()

            # Input signal might be too high and produce diverging values in simulation.
            self.simulate_forward()

            if np.isnan(self.get_xs()).any():  # Detect numerical instability due to too large control update.
                step *= self.factor_down**2  # Double the step for faster search of stable region.
                self.step = step
                print(f"Diverging model output, decrease step size to {step}.")
                self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
                self.update_input()
            else:
                break
        if noisy:
            cost = self.compute_cost_noisy(self.M)
        else:
            cost = (
                self.compute_total_cost()
            )  # Cost after applying control update according to gradient with first valid
        # step size (numerically stable).
        if (
            cost > cost0
        ):  # If the cost choosing the first (stable) step size is no improvement, reduce step size by bisection.
            step, counter = self.decrease_step(cost, cost0, step, control0, self.factor_down, cost_gradient)

        elif (
            cost < cost0
        ):  # If the cost is improved with the first (stable) step size, search for larger steps with even better
            # reduction of cost.

            step, counter = self.increase_step(cost, cost0, step, control0, self.factor_up, cost_gradient)

        else:  # Remark: might be included as part of adaptive search for further improvement.
            step = 0.0  # For later analysis only.
            counter = 0
            self.zero_step_encountered = True

        self.step = step  # Memorize the last step size for the next optimization step with next gradient.

        self.step_sizes_loops_history.append(counter)
        self.step_sizes_history.append(step)

        return step

    def optimize(self, n_max_iterations):
        """Optimization method
            Choose deterministic (M=1 noise realizations) or stochastic (M>1 noise realizations) approach.
            The control-inputs are updated in place throughout the optimization.

        :param n_max_iterations: Maximum number of iterations of gradient descent.
        :type n_max_iterations:  int
        """

        self.cost_interval = convert_interval(
            self.cost_interval, self.T
        )  # Assure check in repeated calls of ".optimize()".

        self.control = update_control_with_limit(
            self.control, 0.0, np.zeros(self.control.shape), self.maximum_control_strength
        )  # To avoid issues in repeated executions.

        if self.M == 1:
            print("Compute control for a deterministic system")
            return self.optimize_deterministic(n_max_iterations)
        else:
            print("Compute control for a noisy system")
            return self.optimize_noisy(n_max_iterations)

    def optimize_deterministic(self, n_max_iterations):
        """Compute the optimal control signal for noise averaging method 0 (deterministic, M=1).

        :param n_max_iterations: maximum number of iterations of gradient descent
        :type n_max_iterations: int
        """

        # (I) forward simulation
        self.simulate_forward()  # yields x(t)

        cost = self.compute_total_cost()
        print(f"Cost in iteration 0: %s" % (cost))
        if len(self.cost_history) == 0:  # add only if control model has not yet been optimized
            self.cost_history.append(cost)

        for i in range(1, n_max_iterations + 1):
            grad = self.compute_gradient()

            if self.zero_step_encountered:
                print(f"Converged in iteration %s with cost %s" % (i, cost))
                break

            self.step_size(-grad)
            self.simulate_forward()

            cost = self.compute_total_cost()
            if i in self.print_array:
                print(f"Cost in iteration %s: %s" % (i, cost))
            self.cost_history.append(cost)

        print(f"Final cost : %s" % (cost))

    def optimize_noisy(self, n_max_iterations):
        """Compute the optimal control signal for noise averaging method 3.

        :param n_max_iterations: maximum number of iterations of gradient descent
        :type n_max_iterations: int
        """

        # initialize array containing M gradients (one per noise realization) for each iteration
        grad_m = np.zeros((self.M, self.N, self.dim_out, self.T))
        consecutive_zero_step = 0

        if len(self.control_history) == 0:
            self.control_history.append(self.control)

        if self.validate_per_step:  # if cost is computed for M_validation realizations in every step
            for m in range(self.M):
                self.simulate_forward()
                grad_m[m, :] = self.compute_gradient()
            cost = self.compute_cost_noisy(self.M_validation)
        else:
            cost_m = 0.0
            for m in range(self.M):
                self.simulate_forward()
                cost_m += self.compute_total_cost()
                grad_m[m, :] = self.compute_gradient()
            cost = cost_m / self.M

        print(f"Mean cost in iteration 0: %s" % (cost))

        if len(self.cost_history) == 0:
            self.cost_history.append(cost)

        for i in range(1, n_max_iterations + 1):

            grad = np.mean(grad_m, axis=0)

            count = 0
            while count < self.count_noisy_step:
                count += 1
                self.zero_step_encountered = False
                _ = self.step_size(-grad)
                if not self.zero_step_encountered:
                    consecutive_zero_step = 0
                    break

            if self.zero_step_encountered:
                consecutive_zero_step += 1
                print("Failed to improve further for noisy system.")

                if consecutive_zero_step > 2:
                    print("Failed to improve further for noisy system three times in a row, stop optimization.")
                    break

            self.control_history.append(self.control)

            if self.validate_per_step:  # if cost is computed for M_validation realizations in every step
                for m in range(self.M):
                    self.simulate_forward()
                    grad_m[m, :] = self.compute_gradient()
                cost = self.compute_cost_noisy(self.M_validation)
            else:
                cost_m = 0.0
                for m in range(self.M):
                    self.simulate_forward()
                    cost_m += self.compute_total_cost()
                    grad_m[m, :] = self.compute_gradient()
                cost = cost_m / self.M

            if i in self.print_array:
                print(f"Mean cost in iteration %s: %s" % (i, cost))
            self.cost_history.append(cost)

        # take most successful control as optimal control
        min_index = np.argmin(self.cost_history)
        oc = self.control_history[min_index]

        print(f"Minimal cost found at iteration %s" % (min_index))

        self.control = oc
        self.update_input()
        self.cost_validation = self.compute_cost_noisy(self.M_validation)
        print(f"Final cost validated with %s noise realizations : %s" % (self.M_validation, self.cost_validation))

    def compute_cost_noisy(self, M):
        """Computes the average cost from 'M_validation' noise realizations.

        :rtype: float
        """
        cost_validation = 0.0
        m = 0
        while m < M:
            self.simulate_forward()
            if np.isnan(self.get_xs()).any():
                continue
            cost_validation += self.compute_total_cost()
            m += 1
        return cost_validation / M
