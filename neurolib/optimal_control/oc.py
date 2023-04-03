import abc
import numba
import numpy as np
from neurolib.optimal_control import cost_functions
import logging
import copy
import random


global limit_control_diff
limit_control_diff = 1e-40


# compared loops agains "@", "np.matmul" and "np.dot": loops ~factor 3.5 faster
@numba.njit
def solve_adjoint(hx, hx_nw, fx, state_dim, dt, N, T):
    """Backwards integration of the adjoint state.
    :param hx: dh/dx    Jacobians for each time step.
    :type hx:           np.ndarray of shape Tx2x2
    :param hx_nw:       Jacobians for each time step for the network coupling.
    :type hx_nw:        np.ndarray
    :param fx: df/dx    Derivative of cost function wrt. to systems dynamics.
    :type fx:           np.ndarray
    :param state_dim:   Dimensions of state (NxVxT).
    :type state_dim:    tuple
    :param dt:          Time resolution of integration.
    :type dt:           float
    :param N:           number of nodes in the network
    :type N:            int
    :param T:           length of simulation (time dimension)
    :type T:            int

    :return:            adjoint state.
    :rtype:             np.ndarray of shape `state_dim`
    """
    # ToDo: generalize, not only precision cost
    adjoint_state = np.zeros(state_dim)
    fx_fullstate = np.zeros(state_dim)
    fx_fullstate[:, :2, :] = fx.copy()

    for ind in range(T - 2, -1, -1):
        for n in range(N):
            der = fx_fullstate[n, :, ind + 1].copy()
            for k in range(len(der)):
                for i in range(len(der)):
                    der[k] += adjoint_state[n, i, ind + 1] * hx[n, ind + 1][i, k]
            for n2 in range(N):
                for k in range(len(der)):
                    for i in range(len(der)):
                        der[k] += adjoint_state[n2, i, ind + 1] * hx_nw[n2, n, ind + 1][i, k]
            adjoint_state[n, :, ind] = adjoint_state[n, :, ind + 1] - der * dt

    return adjoint_state


@numba.njit
def update_control_with_limit(control, step, gradient, u_max):
    """Computes the updated control signal. The absolute values of the new control are bounded by +/- u_max.
    :param control:         N x V x T array. Control signals.
    :type control:          np.ndarray

    :param step:            Step size along the gradients.
    :type step:             float

    :param gradient:        N x V x T array of the gradients.
    :type gradient:         np.ndarray

    :param u_max:           Maximum absolute value allowed for the strength of the control signal.
    :type u_max:            float or None

    :return:                N x V x T array containing the new control signal (updated according to 'step' and
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

    if interval[0] is None:
        interval_0_new = 0
    elif interval[0] < 0:
        assert interval[0] > -array_length, "Interval is not specified in valid range."
        interval_0_new = array_length + interval[0]  # interval entry is negative
    else:
        interval_0_new = interval[0]

    if interval[1] is None:
        interval_1_new = array_length
    elif interval[1] < 0:
        assert interval[1] > -array_length, "Interval is not specified in valid range."
        interval_1_new = array_length + interval[1]  # interval entry is negative
    else:
        interval_1_new = interval[1]

    assert interval_0_new < interval_1_new, "Order of indices for interval is not valid."
    assert interval_1_new <= array_length, "Interval is not specified in valid range."

    return interval_0_new, interval_1_new


class OC:
    def __init__(
        self,
        model,
        target,
        w_p=1.0,
        w_2=1.0,
        w_1=0.0,
        w_1T=0.0,
        w_1D=0.0,
        maximum_control_strength=None,
        print_array=[],
        precision_cost_interval=(None, None),
        control_interval=(None, None),
        precision_matrix=None,
        control_matrix=None,
        M=1,
        M_validation=0,
        validate_per_step=False,
        random_init=None,
    ):
        """
        Base class for optimal control. Model specific methods should be implemented in derived class for each model.

        :param model:       An instance of neurolib's Model-class. Parameters like ".duration" and methods like ".run()"
                            are used within the optimal control.
        :type model:        neurolib.models.model

        :param target:      2xT matrix with [0, :] target of x-population and [1, :] target of y-population.
        :type target:       np.ndarray

        :param w_p:         Weight of the precision cost term, defaults to 1.
        :type w_p:          float, optional

        :param w_2:         Weight of the L2 cost term, defaults to 1.
        :type w_2:          float, optional

        :param w_1:         Weight of the L1 cost term, defaults to 0.
        :type w_1:          float, optional

        :param w_1T:        Weight of the L1 T (temporal sparsity) cost term, defaults to 0.
        :type w_1T:         float, optional

        :param w_1D:        Weight of the L1 D (direcitonal sparsity) cost term, defaults to 0.
        :type w_1D:         float, optional

        :param maximum_control_strength:    Maximum absolute value a control signal can take. No limitation of the
                                            absolute control strength if 'None'. Defaults to None.
        :type:                              float or None, optional

        :param print_array: Array of optimization-iteration-indices (starting at 1) in which cost is printed out.
                            Defaults to empty list `[]`.
        :type print_array:  list, optional

        :param precision_cost_interval: (t_start, t_end). Indices of start and end point (both inclusive) of the
                                        time interval in which the precision cost is evaluated. Default is full time
                                        series. Defaults to (None, None).
        :type precision_cost_interval:  tuple, optional

        :param precision_matrix: NxV binary matrix that defines nodes and channels of precision measurement, defaults to
                                 None
        :type precision_matrix:  np.ndarray

        :param control_matrix: NxV binary matrix that defines active control inputs, defaults to None
        :type control_matrix:  np.ndarray

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

        if self.model.getMaxDelay() > 0.0:
            print("Delay not yet implemented, please set delays to zero")

        self.target = target  # ToDo: dimensions-check

        self.w_p = w_p
        self.w_2 = w_2
        self.w_1 = w_1
        self.w_1T = w_1T
        self.w_1D = w_1D
        self.maximum_control_strength = maximum_control_strength

        self.step = 10.0
        if self.model.name == "wc":
            self.step = 1000.0
        self.count_noisy_step = 10
        self.count_step = 20

        self.N = self.model.params.N

        self.dim_vars = len(self.model.state_vars)
        self.dim_out = len(self.model.output_vars)
        self.dim_in = len(self.model.input_vars)

        if self.N > 1:  # check that coupling matrix has zero diagonal
            assert np.all(np.diag(self.model.Cmat) == 0.0)
        elif self.N == 1:
            if type(self.model.Cmat) == type(None):
                self.model.Cmat = np.zeros((self.N, self.N))
            if type(self.model.Dmat) == type(None):
                self.model.Dmat = np.zeros((self.N, self.N))

        self.Dmat_ndt = np.around(self.model.Dmat / self.model.params.dt).astype(int)

        self.precision_matrix = precision_matrix
        if isinstance(self.precision_matrix, type(None)):
            self.precision_matrix = np.ones(
                (self.N, self.dim_out)
            )  # default: measure precision in all variables in all nodes

        # check if matrix is binary
        assert np.array_equal(self.precision_matrix, self.precision_matrix.astype(bool))

        self.control_matrix = control_matrix
        if isinstance(self.control_matrix, type(None)):
            self.control_matrix = np.ones((self.N, self.dim_vars))  # default: all channels in all nodes active

        # check if matrix is binary
        assert np.array_equal(self.control_matrix, self.control_matrix.astype(bool))

        self.M = max(1, M)
        self.M_validation = max(M, M_validation)
        self.cost_validation = 0.0
        self.validate_per_step = validate_per_step

        self.M_states = [None] * self.M

        self.random_init = random_init

        self.check_random_init()

        if type(self.random_init) == type(None):

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
                if self.M > 1 or self.M_validation < self.M or self.validate_per_step:
                    print(
                        'For deterministic systems, parameters "M", "M_validation" and "validate_per_step" are not relevant.'
                        + "\n"
                        + 'If you want to study a noisy system, please set model parameter "sigma_ou" larger than zero'
                    )

        else:

            if self.M <= 1:
                logging.warning(
                    "For random initializations, please chose parameter M larger than 1 (recommendation > 10)."
                )
            if self.M > self.M_validation:
                print('Parameter "M_validation" should be chosen larger than parameter "M".')

        self.dt = self.model.params["dt"]  # maybe redundant but for now code clarity
        self.duration = self.model.params["duration"]  # maybe redundant but for now code clarity

        self.T = np.around(self.duration / self.dt, 0).astype(int) + 1  # Total number of time steps is
        # initial condition.

        # + forward simulation steps of neurolibs model.run().
        self.state_dim = (
            self.N,
            self.dim_vars,
            self.T,
        )  # dimensions of state. Model has N network nodes, V state variables, T time points

        self.adjoint_state = np.zeros(self.state_dim)
        self.grad = np.zeros(self.state_dim)

        # check correct specification of inputs
        # ToDo: different models have different inputs
        self.control = None

        self.cost_history = []
        self.step_sizes_history = []
        self.step_sizes_loops_history = []

        # save control signals throughout optimization iterations for later analysis
        self.control_history = []

        self.print_array = print_array

        self.zero_step_encountered = False  # deterministic gradient descent cannot further improve

        self.precision_cost_interval = convert_interval(precision_cost_interval, self.T)
        self.control_interval = convert_interval(control_interval, self.T)

        self.step_factor = 0.5

    @abc.abstractmethod
    def get_xs(self):
        """Stack the initial condition with the simulation results for both populations."""
        pass

    def simulate_forward(self):
        """Updates self.xs in accordance to the current self.control.
        Results can be accessed with self.get_xs()
        """
        self.model.run()

    @abc.abstractmethod
    def update_input(self):
        """Update the parameters in self.model according to the current control such that self.simulate_forward
        operates with the appropriate control signal.
        """
        pass

    @abc.abstractmethod
    def Dxdot(self):
        """2x2 Jacobian of systems dynamics wrt. to change of systems variables."""
        # ToDo: model dependent
        pass

    @abc.abstractmethod
    def Duh(self):
        """Jacobian of systems dynamics wrt. to I_ext (external control input)"""
        # ToDo: model dependent
        pass

    def compute_total_cost(self):
        """Compute the total cost as weighted sum precision of precision and L2 term."""
        cost = 0.0
        if self.w_p != 0:
            cost += cost_functions.precision_cost(
                self.target,
                self.get_xs(),
                self.w_p,
                self.precision_matrix,
                self.dt,
                self.precision_cost_interval,
            )
        if self.w_2 != 0.0:
            cost += cost_functions.energy_cost(self.control, self.w_2, self.dt)
        if self.w_1 != 0.0:
            cost += cost_functions.L1_cost(self.control, self.w_1, self.dt)
        if self.w_1T != 0.0:
            cost += cost_functions.L1T_cost(self.control, self.w_1T, self.dt)
        if self.w_1D != 0.0:
            cost += cost_functions.L1D_cost(self.control, self.w_1D, self.dt)
        return cost

    @abc.abstractmethod
    def compute_gradient(self):
        """Du @ fk + adjoint_k.T @ Du @ h"""
        # ToDo: model dependent
        pass

    @abc.abstractmethod
    def compute_hx(self):
        """Jacobians for each time step.

        :return: Array of length self.T containing 2x2-matrices
        :rtype: np.ndarray
        """
        pass

    @abc.abstractmethod
    def compute_hx_nw(self):
        """Jacobians for each time step for the network coupling

        :return: (N x self.T x (2x2) array
        :rtype: np.ndarray
        """
        pass

    def solve_adjoint(self):
        """Backwards integration of the adjoint state."""
        hx = self.compute_hx()
        hx_nw = self.compute_hx_nw()

        # ToDo: generalize, not only precision cost
        fx = cost_functions.derivative_precision_cost(
            self.target,
            self.get_xs(),
            self.w_p,
            self.precision_matrix,
            self.precision_cost_interval,
        )

        self.adjoint_state = solve_adjoint(hx, hx_nw, fx, self.state_dim, self.dt, self.N, self.T)

    def step_size(self, cost_gradient):
        """Use cost_gradient to avoid unnecessary re-computations (also of the adjoint state)
        :param cost_gradient:
        :type cost_gradient:

        :return:    Step size that got multiplied with the cost_gradient.
        :rtype:     float
        """
        self.simulate_forward()
        cost0 = self.compute_total_cost()
        factor = self.step_factor
        step = self.step
        counter = 0.0

        control0 = self.control

        while True:
            # inplace updating of models x_ext bc. forward-sim relies on models parameters
            self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
            self.update_input()

            # input signal might be too high and produce diverging values in simulation
            self.simulate_forward()
            if np.isnan(self.get_xs()).any():
                step *= factor
                self.step = step
                print("diverging model output, decrease step size to ", step)
                self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
                self.update_input()
            else:
                break

        cost = self.compute_total_cost()
        while cost > cost0:
            step *= factor
            counter += 1

            # inplace updating of models x_ext bc. forward-sim relies on models parameters
            self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
            self.update_input()

            self.simulate_forward()
            cost = self.compute_total_cost()

            if counter == self.count_step:
                step = 0.0  # for later analysis only
                self.control = update_control_with_limit(
                    control0, 0.0, np.zeros(control0.shape), self.maximum_control_strength
                )
                self.update_input()
                cost = cost0
                # logging.warning("Zero step encoutered, stop bisection")
                self.zero_step_encountered = True
                break

        cost_node = np.zeros((self.N, self.dim_in))
        step_node = np.ones((self.N, self.dim_in)) * self.step
        for n in range(self.N):
            for v in range(self.dim_in):
                gradient_node = np.zeros((cost_gradient.shape))
                gradient_node[n, v, :] = cost_gradient[n, v, :]
                self.control = update_control_with_limit(
                    control0, step_node[n, v], gradient_node, self.maximum_control_strength
                )
                self.update_input()
                self.simulate_forward()
                cost_node[n, v] = self.compute_total_cost()
                counter = 0.0

                while cost_node[n, v] > cost0:
                    step_node[n, v] *= factor
                    counter += 1

                    # inplace updating of models x_ext bc. forward-sim relies on models parameters
                    self.control = update_control_with_limit(
                        control0, step_node[n, v], gradient_node, self.maximum_control_strength
                    )
                    self.update_input()
                    self.simulate_forward()
                    cost_node[n, v] = self.compute_total_cost()

                    if counter == self.count_step:
                        step_node[n, v] = 0.0  # for later analysis only
                        self.control = update_control_with_limit(
                            control0, 0.0, np.zeros(control0.shape), self.maximum_control_strength
                        )
                        self.update_input()
                        # logging.warning("Zero step encoutered, stop bisection")
                        break

        grad = cost_gradient.copy()
        s = step
        min_ind = 0
        if np.amin(cost_node) < cost:
            min_ind = np.where(cost_node == np.amin(cost_node))
            gradient_node = np.zeros((cost_gradient.shape))
            gradient_node[min_ind[0][0], min_ind[1][0], :] = cost_gradient[min_ind[0][0], min_ind[1][0], :]
            grad = gradient_node.copy()
            s = step_node[min_ind[0][0], min_ind[1][0]]

        if self.zero_step_encountered:
            if np.amax(step_node) > 0.0:
                self.zero_step_encountered = False

        self.control = update_control_with_limit(control0, s, grad, self.maximum_control_strength)
        self.update_input()
        self.simulate_forward()

        # self.step_sizes_loops_history.append(counter)
        self.step_sizes_history.append(s)

        return s

    def optimize(self, n_max_iterations):
        """Optimization method
            chose from deterministic (M=1) or one of several stochastic (M>1) approaches

        :param n_max_iterations: maximum number of iterations of gradient descent
        :type n_max_iterations: int

        """

        self.precision_cost_interval = convert_interval(
            self.precision_cost_interval, self.T
        )  # To avoid issues in repeated executions.

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

        self.zero_step_encountered = False

        for i in range(1, n_max_iterations + 1):
            self.grad = self.compute_gradient()

            if np.amax(np.abs(self.grad)) < limit_control_diff:
                print(f"converged with vanishing gradient in iteration %s with cost %s" % (i, cost))
                self.zero_step_encountered = True
                break

            c0 = self.control.copy()
            self.step_size(-self.grad)

            if self.zero_step_encountered:
                print(f"Converged in iteration %s with cost %s because of step counter" % (i, cost))
                if cost != self.cost_history[-1]:
                    self.cost_history.append(cost)
                break

            if np.amax(np.abs(self.control - c0)) < limit_control_diff:  # ToDo : self.limit
                print(f"Converged in iteration %s with cost %s because of vanishing difference" % (i, cost))
                if cost != self.cost_history[-1]:
                    self.cost_history.append(cost)
                break

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
                self.set_random_init()
                self.simulate_forward()
                self.M_states[m] = self.get_xs()
                grad_m[m, :] = self.compute_gradient()
            cost = self.compute_cost_noisy(self.M_validation)
        else:
            cost_m = 0.0
            for m in range(self.M):
                self.set_random_init()
                self.simulate_forward()
                self.M_states[m] = self.get_xs()
                cost_m += self.compute_total_cost()
                grad_m[m, :] = self.compute_gradient()
            cost = cost_m / self.M

        print(f"Mean cost in iteration 0: %s" % (cost))

        if len(self.cost_history) == 0:
            self.cost_history.append(cost)

        for i in range(1, n_max_iterations + 1):

            self.grad = np.mean(grad_m, axis=0)

            count = 0
            while count < self.count_noisy_step:
                count += 1
                self.zero_step_encountered = False
                _ = self.step_size_noisy(-self.grad)
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
                    self.set_random_init()
                    self.simulate_forward()
                    self.M_states[m] = self.get_xs()
                    grad_m[m, :] = self.compute_gradient()
                cost = self.compute_cost_noisy(self.M_validation)
            else:
                cost_m = 0.0
                for m in range(self.M):
                    self.set_random_init()
                    self.simulate_forward()
                    self.M_states[m] = self.get_xs()
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
        """Computes the average cost from M_validation noise realizations."""
        cost_validation = 0.0
        m = 0
        while m < M:
            self.set_random_init()
            self.simulate_forward()
            if np.isnan(self.get_xs()).any():
                continue
            cost_validation += self.compute_total_cost()
            m += 1

        return cost_validation / M

    def step_size_noisy(self, cost_gradient):
        """Use cost_gradient to avoid unnecessary re-computations (also of the adjoint state)
        :param cost_gradient:
        :type cost_gradient:

        :return:    Step size that got multiplied with the cost_gradient.
        :rtype:     float
        """
        self.set_random_init()
        self.simulate_forward()
        cost0 = self.compute_cost_noisy(self.M)

        factor = 0.5
        step = self.step
        counter = 0.0

        control0 = self.control

        while True:
            # inplace updating of models x_ext bc. forward-sim relies on models parameters
            self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
            self.update_input()

            # input signal might be too high and produce diverging values in simulation
            self.set_random_init()
            self.simulate_forward()
            if np.isnan(self.get_xs()).any():
                step *= factor * factor  # decrease step twice to avoid that other noise realizations diverge
                self.step = step
                print("diverging model output, decrease step size to ", step)
                self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
                self.update_input()
            else:
                break

        cost = self.compute_cost_noisy(self.M)
        while cost > cost0:
            step *= factor
            counter += 1

            # inplace updating of models x_ext bc. forward-sim relies on models parameters
            self.control = update_control_with_limit(control0, step, cost_gradient, self.maximum_control_strength)
            self.update_input()

            cost = self.compute_cost_noisy(self.M)

            if counter == self.count_step:
                step = 0.0  # for later analysis only
                self.control = update_control_with_limit(
                    control0, 0.0, np.zeros(control0.shape), self.maximum_control_strength
                )
                self.update_input()
                self.zero_step_encountered = True

        self.step_sizes_loops_history.append(counter)
        self.step_sizes_history.append(step)

        return step

    def set_random_init(self):

        if type(self.random_init) == type(None):
            return

        for n in range(self.N):
            rand_index = np.random.randint(low=0, high=self.random_init.shape[2])

            for iv_index in range(len(self.model.init_vars)):
                ri = self.random_init[n, iv_index, rand_index]

                if self.model.params[self.model.init_vars[iv_index]].ndim == 2:
                    self.model.params[self.model.init_vars[iv_index]] = np.array([[ri]])
                elif self.model.params[self.model.init_vars[iv_index]].ndim == 1:
                    self.model.params[self.model.init_vars[iv_index]] = np.ones((self.N,)) * ri

        return

    def check_random_init(self):
        if type(self.random_init) == type(None):
            return

        assert self.random_init.shape[0] == self.N, "Please indicate ellipse for random initialization for each node."
        assert self.random_init.shape[1] == len(
            self.model.init_vars
        ), "Please indicate ellipse for random initialization in each dimension of initial values."
