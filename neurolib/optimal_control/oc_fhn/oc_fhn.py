from neurolib.optimal_control.oc import OC, update_control_with_limit
from neurolib.optimal_control import cost_functions
import numpy as np
import numba


@numba.njit
def jacobian_fhn(alpha, beta, gamma, tau, epsilon, x, V):
    """Jacobian of a single node of the FHN dynamical system wrt. to its 'state_vars' ('x', 'y', 'x_ou', 'y_ou'). The
       Jacobian of the FHN systems dynamics depends only on the constant model parameters and the values of the 'x'-
       population.

    :param alpha:   FHN model parameter.
    :type alpha:    float
    :param beta:    FHN model parameter.
    :type beta:     float
    :param gamma:   FHN model parameter.
    :type gamma:    float
    :param tau:     FHN model parameter.
    :type tau:      float
    :param epsilon: FHN model parameter.
    :type epsilon:  float
    :param x:       Value of the 'x'-population in the FHN node at a specific time step.
    :type x:        float
    :param V:       Number of system variables.
    :type V:        int
    :return:        4 x 4 Jacobian matrix.
    :rtype:         np.ndarray
    """
    jacobian = np.zeros((V, V))
    jacobian[0, :2] = [3 * alpha * x**2 - 2 * beta * x - gamma, 1]
    jacobian[1, :2] = [-1 / tau, epsilon / tau]
    return jacobian


@numba.njit
def compute_hx(alpha, beta, gamma, tau, epsilon, N, V, T, dyn_vars):
    """Jacobians  of FHN model wrt. to its 'state_vars' at each time step.

    :param alpha:   FHN model parameter.
    :type alpha:    float
    :param beta:    FHN model parameter.
    :type beta:     float
    :param gamma:   FHN model parameter.
    :type gamma:    float
    :param tau:     FHN model parameter.
    :type tau:      float
    :param epsilon: FHN model parameter.
    :type epsilon:  float
    :param N:       Number of nodes in the network.
    :type N:        int
    :param V:       Number of system variables.
    :type V:        int
    :param T:       Length of simulation (time dimension).
    :type T:        int
    :param dyn_vars:      Values of the 'x' and 'y' variable of FHN of all nodes through time.
    :type dyn_vars:       np.ndarray of shape N x 2 x T
    :return:        Array that contains Jacobians for all nodes in all time steps.
    :rtype:         np.ndarray of shape N x T x 4 x 4
    """
    hx = np.zeros((N, T, V, V))

    for n in range(N):  # Iterate through nodes.
        for ind, x in enumerate(dyn_vars[n, 0, :]):  # Pick value of x-variable at each time step.
            hx[n, ind, :, :] = jacobian_fhn(alpha, beta, gamma, tau, epsilon, x, V)
    return hx


@numba.njit
def compute_hx_nw(K_gl, cmat, coupling, N, V, T):
    """Jacobians for network connectivity in all time steps.

    :param K_gl:     Model parameter of global coupling strength.
    :type K_gl:      float
    :param cmat:     Model parameter, connectivity matrix.
    :type cmat:      ndarray
    :param coupling: Model parameter, which specifies the coupling type. E.g. "additive" or "diffusive".
    :type coupling:  str
    :param N:        Number of nodes in the network.
    :type N:         int
    :param V:        Number of system variables.
    :type V:         int
    :param T:        Length of simulation (time dimension).
    :type T:         int
    :return:         Jacobians for network connectivity in all time steps.
    :rtype:          np.ndarray of shape N x N x T x 4 x 4
    """
    hx_nw = np.zeros((N, N, T, V, V))

    for n1 in range(N):
        for n2 in range(N):
            hx_nw[n1, n2, :, 0, 0] = K_gl * cmat[n1, n2]  # term corresponding to additive coupling
            if coupling == "diffusive":
                hx_nw[n1, n1, :, 0, 0] += -K_gl * cmat[n1, n2]

    return -hx_nw


class OcFhn(OC):
    """Class for optimal control specific to neurolib's implementation of the FitzHugh-Nagumo (FHN) model.

    :param model: Instance of FHN model (can describe a single FHN node or a network of coupled FHN nodes. Remark:
                  Currently only delay-free networks are supported.
    :type model:  neurolib.models.fhn.model.FHNModel
    """

    def __init__(
        self,
        model,
        target,
        weights=None,
        maximum_control_strength=None,
        print_array=[],
        precision_cost_interval=(None, None),
        precision_matrix=None,
        control_matrix=None,
        M=1,
        M_validation=0,
        validate_per_step=False,
    ):
        super().__init__(
            model,
            target,
            weights=weights,
            maximum_control_strength=maximum_control_strength,
            print_array=print_array,
            precision_cost_interval=precision_cost_interval,
            precision_matrix=precision_matrix,
            control_matrix=control_matrix,
            M=M,
            M_validation=M_validation,
            validate_per_step=validate_per_step,
        )

        assert self.model.name == "fhn"

        assert self.T == self.model.params["x_ext"].shape[1]
        assert self.T == self.model.params["y_ext"].shape[1]

        # ToDo: here, a method like neurolib.model_utils.adjustArrayShape() should be applied!
        if self.N == 1:  # single-node model
            if self.model.params["x_ext"].ndim == 1:
                print("not implemented yet")
            else:
                control = np.concatenate((self.model.params["x_ext"], self.model.params["y_ext"]), axis=0)[
                    np.newaxis, :, :
                ]
        else:
            control = np.stack((self.model.params["x_ext"], self.model.params["y_ext"]), axis=1)

        for n in range(self.N):
            assert (control[n, 0, :] == self.model.params["x_ext"][n, :]).all()
            assert (control[n, 1, :] == self.model.params["y_ext"][n, :]).all()

        self.control = update_control_with_limit(control, 0.0, np.zeros(control.shape), self.maximum_control_strength)

    def get_xs(self):
        """Stack the initial condition with the simulation results for dynamic variables 'x' and 'y' of FHN Model.

        :rtype:     np.ndarray of shape N x V x T
        """
        return np.concatenate(
            (
                np.concatenate((self.model.params["xs_init"], self.model.params["ys_init"]), axis=1)[:, :, np.newaxis],
                np.stack((self.model.x, self.model.y), axis=1),
            ),
            axis=2,
        )

    def update_input(self):
        """Update the parameters in 'self.model' according to the current control such that 'self.simulate_forward'
        operates with the appropriate control signal.
        """
        # ToDo: find elegant way to combine the cases
        if self.N == 1:
            self.model.params["x_ext"] = self.control[:, 0, :].reshape(1, -1)  # Reshape as row vector to match access
            self.model.params["y_ext"] = self.control[:, 1, :].reshape(1, -1)  # in model's time integration.

        else:
            self.model.params["x_ext"] = self.control[:, 0, :]
            self.model.params["y_ext"] = self.control[:, 1, :]

    def Dxdot(self):
        """V x V Jacobian of systems dynamics wrt. to change of systems variables."""
        # Currently not explicitly required since it is identity matrix.
        raise NotImplementedError  # return np.eye(4)

    def Duh(self):
        """V x V Jacobian of systems dynamics wrt. to external inputs (control signals) to all 'state_vars'. There are no
           inputs to the noise variables 'x_ou' and 'y_ou' in the model.

        :rtype:     np.ndarray of shape V x V
        """
        return np.array([[-1, 0, 0, 0], [0, -1, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]])

    def compute_hx(self):
        """Jacobians of FHN model wrt. to its 'state_vars' at each time step.

        :return:        Array that contains Jacobians for all nodes in all time steps.
        :rtype:         np.ndarray of shape N x T x V x V
        """
        return compute_hx(
            self.model.params["alpha"],
            self.model.params["beta"],
            self.model.params["gamma"],
            self.model.params["tau"],
            self.model.params["epsilon"],
            self.N,
            self.dim_vars,
            self.T,
            self.get_xs(),
        )

    def compute_hx_nw(self):
        """Jacobians for each time step for the network coupling.

        :return:    Jacobians for network connectivity in all time steps.
        :rtype:     np.ndarray of shape N x N x T x V x V
        """
        return compute_hx_nw(
            self.model.params["K_gl"],
            self.model.params["Cmat"],
            self.model.params["coupling"],
            self.N,
            self.dim_vars,
            self.T,
        )
