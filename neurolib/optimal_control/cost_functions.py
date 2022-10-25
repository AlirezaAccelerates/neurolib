import numpy as np
import numba


@numba.njit
def precision_cost(dt, x_target, x_sim, w_p, N, precision_matrix, interval=(0, None)):
    """Summed squared difference between target and simulation within specified time interval weighted by w_p.

    :param x_target:    Control-dimensions x T array that contains the target time series.
    :type x_target:     np.ndarray

    :param x_sim:       Control-dimensions x T array that contains the simulated time series.
    :type x_sim:        np.ndarray

    :param w_p:         Weight that is multiplied with the precision cost.
    :type w_p:          float

    :param N:           Number of nodes.
    :type N:            int

    :param precision_matrix: NxV binary matrix that defines nodes and channels of precision measurement, defaults to
                                 None
    :type precision_matrix:  np.ndarray

    :param interval:    [t_start, t_end]. Indices of start and end point of the slice (both inclusive) in time
                        dimension. Default is full time series, defaults to (0, None).
    :type interval:     tuple, optional

    :return:            Precision cost for time interval.
    :rtype:             float

    """
    # np.sum without specified axis implicitly performs
    # summation that would correspond to np.sum((x1(t)-x2(t)**2)
    # for the norm at one particular t as well as the integration over t
    # (commutative)

    # ToDo: remove parameter N

    cost = 0.0
    for n in range(N):
        for v in range(x_target.shape[1]):
            for t in range(interval[0], interval[1]):
                cost += precision_matrix[n, v] * (x_target[n, v, t] - x_sim[n, v, t]) ** 2

    return w_p * dt * 0.5 * cost


@numba.njit
def derivative_precision_cost(x_target, x_sim, w_p, precision_matrix, interval=(0, None)):
    """Derivative of precision cost wrt. to x_sim.

    :param x_target:    Control-dimensions x T array that contains the target time series.
    :type x_target:     np.ndarray

    :param x_sim:       Control-dimensions x T array that contains the simulated time series.
    :type x_sim:        np.ndarray

    :param w_p:         Weight that is multiplied with the precision cost.
    :type w_p:          float

    :param precision_matrix: NxV binary matrix that defines nodes and channels of precision measurement, defaults to
                                 None
    :type precision_matrix:  np.ndarray

    :param interval:    [t_start, t_end]. Indices of start and end point of the slice (both inclusive) in time
                        dimension. Default is full time series, defaults to (0, None).
    :type interval:     tuple, optional

    :return:            Control-dimensions x T array of precision cost gradients.
    :rtype:             np.ndarray
    """

    derivative = np.zeros(x_target.shape)

    for n in range(x_target.shape[0]):
        for v in range(x_target.shape[1]):
            for t in range(interval[0], interval[1]):  # [:, :, interval[0] : interval[1]]
                derivative[n, v, t] = -w_p * (x_target[n, v, t] - x_sim[n, v, t])

    for n in range(x_target.shape[0]):
        for v in range(x_target.shape[1]):
            for t in range(interval[0], interval[1]):
                derivative[n, v, t] = np.multiply(derivative[n, v, t], precision_matrix[n, v])
    return derivative


# @numba.njit
def energy_cost(dt, u, w_2):
    """
    :param u:   Control-dimensions x T array. Control signals.
    :type u:    np.ndarray

    :param w_2: Weight that is multiplied with the W2 ("energy") cost.
    :type w_2:  float

    :return:    W2 cost of the control.
    :rtype:     float
    """
    return w_2 * dt * 0.5 * np.sum(u**2.0)


@numba.njit
def derivative_energy_cost(u, w_2):
    """
    :param u:   Control-dimensions x T array. Control signals.
    :type u:    np.ndarray

    :param w_2: Weight that is multiplied with the W2 ("energy") cost.
    :type w_2:  float

    :return :   Control-dimensions x T array of W2-cost gradients.
    :rtype:     np.ndarray
    """
    return w_2 * u
