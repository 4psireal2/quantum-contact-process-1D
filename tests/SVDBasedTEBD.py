import logging
import time
import warnings

import numpy as np

from tenpy.linalg import np_conserved as npc
from tenpy.algorithms.truncation import svd_theta, TruncationError
from tenpy.algorithms.algorithm import TimeEvolutionAlgorithm

logger = logging.getLogger(__name__)


class TEBDEngine(TimeEvolutionAlgorithm):
    """Time Evolving Block Decimation (TEBD) algorithm.

    Parameters are the same as for :class:`~tenpy.algorithms.algorithm.Algorithm`.

    .. deprecated :: 0.6.0
        Renamed parameter/attribute `TEBD_params` to :attr:`options`.

    Options
    -------
    .. cfg:config :: TEBDEngine
        :include: TimeEvolutionAlgorithm

        start_trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
            Initial truncation error for :attr:`trunc_err`.
        order : int
            Order of the algorithm. The total error for evolution up to a fixed time `t`
            scales as ``O(t*dt^order)``.
        E_offset : None | list of float
            Energy offset to be applied in :meth:`calc_U`, see doc there.
            Only used for real-time evolution!

    Attributes
    ----------
    trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
        The error of the represented state which is introduced due to the truncation during
        the sequence of update steps.
    psi : :class:`~tenpy.networks.mps.MPS`
        The MPS, time evolved in-place.
    model : :class:`~tenpy.models.model.NearestNeighborModel`
        The model defining the Hamiltonian.
    _U : list of list of :class:`~tenpy.linalg.np_conserved.Array`
        Exponentiated `H_bond` (bond Hamiltonians), i.e. roughly ``exp(-i H_bond dt_i)``.
        First list for different `dt_i` as necessary for the chosen `order`,
        second list for the `L` different bonds.
    _U_param : dict
        A dictionary containing the information of the latest created `_U`.
        We don't recalculate `_U` if those parameters didn't change.
    _trunc_err_bonds : list of :class:`~tenpy.algorithms.truncation.TruncationError`
        The *local* truncation error introduced at each bond, ignoring the errors at other bonds.
        The `i`-th entry is left of site `i`.
    _update_index : None | (int, int)
        The indices ``i_dt,i_bond`` of ``U_bond = self._U[i_dt][i_bond]`` during update_step.
    """

    def __init__(self, psi, model, options, **kwargs):
        TimeEvolutionAlgorithm.__init__(self, psi, model, options, **kwargs)
        self._trunc_err_bonds = [TruncationError() for i in range(psi.L + 1)]
        self._U = None
        self._U_param = {}
        self._update_index = None

    @property
    def TEBD_params(self):
        warnings.warn("renamed self.TEBD_params -> self.options", FutureWarning, stacklevel=2)
        return self.options

    @property
    def trunc_err_bonds(self):
        """truncation error introduced on each non-trivial bond."""
        return self._trunc_err_bonds[self.psi.nontrivial_bonds]

    # run() as defined in TimeEvolutionAlgorithm

    def run_GS(self):
        """TEBD algorithm in imaginary time to find the ground state.

        .. note ::
            It is almost always more efficient (and hence advisable) to use DMRG.
            This algorithms can nonetheless be used quite well as a benchmark and for comparison.

        .. cfg:configoptions :: TEBDEngine

            delta_tau_list : list
                A list of floats: the timesteps to be used.
                Choosing a large timestep `delta_tau` introduces large (Trotter) errors,
                but a too small time step requires a lot of steps to reach
                ``exp(-tau H) --> |psi0><psi0|``.
                Therefore, we start with fairly large time steps for a quick time evolution until
                convergence, and then gradually decrease the time step.
            order : int
                Order of the Suzuki-Trotter decomposition.
            N_steps : int
                Number of steps before measurement can be performed
        """
        # initialize parameters
        delta_tau_list = self.options.get(
            'delta_tau_list', [0.1, 0.01, 0.001, 1.e-4, 1.e-5, 1.e-6, 1.e-7, 1.e-8, 1.e-9, 1.e-10, 1.e-11, 0.])
        max_error_E = self.options.get('max_error_E', 1.e-13)
        N_steps = self.options.get('N_steps', 10)
        TrotterOrder = self.options.get('order', 2)

        Eold = np.mean(self.model.bond_energies(self.psi))
        Sold = np.mean(self.psi.entanglement_entropy())
        start_time = time.time()

        for delta_tau in delta_tau_list:
            logger.info("delta_tau=%e", delta_tau)
            self.calc_U(TrotterOrder, delta_tau, type_evo='imag')
            DeltaE = 2 * max_error_E
            step = 0
            while (DeltaE > max_error_E):
                if self.psi.finite and TrotterOrder == 2:
                    self.update_imag(N_steps)
                else:
                    self.evolve(N_steps, delta_tau)
                step += N_steps
                E = np.mean(self.model.bond_energies(self.psi))
                DeltaE = abs(Eold - E)
                Eold = E
                S = self.psi.entanglement_entropy()
                max_S = max(S)
                S = np.mean(S)
                DeltaS = S - Sold
                Sold = S
                logger.info(
                    "--> step=%(step)6d, beta=%(beta)3.3f, max(chi)=%(max_chi)d,"
                    "DeltaE=%(dE).2e, E_bond=%(E).10f, Delta_S=%(dS).4e, "
                    "max(S)=%(max_S).10f, time simulated: %(wall_time).1fs", {
                        'step': step,
                        'beta': -self.evolved_time.imag,
                        'max_chi': max(self.psi.chi),
                        'dE': DeltaE,
                        'E': E.real,
                        'dS': DeltaS,
                        'max_S': max_S,
                        'wall_time': time.time() - start_time,
                    })
        # done

    @staticmethod
    def suzuki_trotter_time_steps(order):
        """Return time steps of U for the Suzuki Trotter decomposition of desired order.

        See :meth:`suzuki_trotter_decomposition` for details.

        Parameters
        ----------
        order : int
            The desired order of the Suzuki-Trotter decomposition.

        Returns
        -------
        time_steps : list of float
            We need ``U = exp(-i H_{even/odd} delta_t * dt)`` for the `dt` returned in this list.
        """
        if order == 1:
            return [1.]
        elif order == 2:
            return [0.5, 1.]
        elif order == 4:
            t1 = 1. / (4. - 4.**(1 / 3.))
            t3 = 1. - 4. * t1
            return [t1 / 2., t1, (t1 + t3) / 2., t3]
        elif order == '4_opt':
            # Eq (30a) of arXiv:1901.04974
            a1 = 0.095848502741203681182
            b1 = 0.42652466131587616168
            a2 = -0.078111158921637922695
            b2 = -0.12039526945509726545
            return [a1, b1, a2, b2, 0.5 - a1 - a2, 1. - 2 * (b1 + b2)]  # a1 b1 a2 b2 a3 b3
        # else
        raise ValueError("Unknown order %r for Suzuki Trotter decomposition" % order)

    @staticmethod
    def suzuki_trotter_decomposition(order, N_steps):
        r"""Returns list of necessary steps for the suzuki trotter decomposition.

        We split the Hamiltonian as :math:`H = H_{even} + H_{odd} = H[0] + H[1]`.
        The Suzuki-Trotter decomposition is an approximation
        :math:`\exp(t H) \approx prod_{(j, k) \in ST} \exp(d[j] t H[k]) + O(t^{order+1 })`.

        Parameters
        ----------
        order : ``1, 2, 4, '4_opt'``
            The desired order of the Suzuki-Trotter decomposition.
            Order ``1`` approximation is simply :math:`e^A a^B`.
            Order ``2`` is the "leapfrog" `e^{A/2} e^B e^{A/2}`.
            Order ``4`` is the fourth-order from :cite:`suzuki1991` (also referenced in
            :cite:`schollwoeck2011`), and ``'4_opt'`` gives the optimized version of Equ. (30a) in
            :cite:`barthel2020`.

        Returns
        -------
        ST_decomposition : list of (int, int)
            Indices ``j, k`` of the time-steps ``d = suzuki_trotter_time_step(order)`` and
            the decomposition of `H`.
            They are chosen such that a subsequent application of ``exp(d[j] t H[k])`` to a given
            state ``|psi>`` yields ``(exp(N_steps t H[k]) + O(N_steps t^{order+1}))|psi>``.
        """
        even, odd = 0, 1
        if N_steps == 0:
            return []
        if order == 1:
            a = (0, odd)
            b = (0, even)
            return [a, b] * N_steps
        elif order == 2:
            a = (0, odd)  # dt/2
            a2 = (1, odd)  # dt
            b = (1, even)  # dt
            # U = [a b a]*N
            #   = a b [a2 b]*(N-1) a
            return [a, b] + [a2, b] * (N_steps - 1) + [a]
        elif order == 4:
            a = (0, odd)  # t1/2
            a2 = (1, odd)  # t1
            b = (1, even)  # t1
            c = (2, odd)  # (t1 + t3) / 2 == (1 - 3 * t1)/2
            d = (3, even)  # t3 = 1 - 4 * t1
            # From Schollwoeck 2011 (:arxiv:`1008.3477`):
            # U = U(t1) U(t2) U(t3) U(t2) U(t1)
            # with U(dt) = U(dt/2, odd) U(dt, even) U(dt/2, odd) and t1 == t2
            # Using above definitions, we arrive at:
            # U = [a b a2 b c d c b a2 b a] * N
            #   = [a b a2 b c d c b a2 b] + [a2 b a2 b c d c b a2 b a] * (N-1) + [a]
            steps = [a, b, a2, b, c, d, c, b, a2, b]
            steps = steps + [a2, b, a2, b, c, d, c, b, a2, b] * (N_steps - 1)
            steps = steps + [a]
            return steps
        elif order == '4_opt':
            # symmetric: a1 b1 a2 b2 a3 b3 a2 b2 a2 b1 a1
            steps = [(0, odd), (1, even), (2, odd), (3, even), (4, odd),  (5, even),
                     (4, odd), (3, even), (2, odd), (1, even), (0, odd)]  # yapf: disable
            return steps * N_steps
        # else
        raise ValueError("Unknown order {0!r} for Suzuki Trotter decomposition".format(order))

    def prepare_evolve(self, dt):
        order = self.options.get('order', 2)
        E_offset = self.options.get('E_offset', None)
        self.calc_U(order, dt, type_evo='real', E_offset=E_offset)

    def calc_U(self, order, delta_t, type_evo='real', E_offset=None):
        """Calculate ``self.U_bond`` from ``self.model.H_bond``.

        This function calculates

        * ``U_bond = exp(-i dt (H_bond-E_offset_bond))`` for ``type_evo='real'``, or
        * ``U_bond = exp(- dt H_bond)`` for ``type_evo='imag'``.

        For first order (in `delta_t`), we need just one ``dt=delta_t``.
        Higher order requires smaller `dt` steps, as given by :meth:`suzuki_trotter_time_steps`.

        Parameters
        ----------
        order : int
            Trotter order calculated U_bond. See update for more information.
        delta_t : float
            Size of the time-step used in calculating U_bond
        type_evo : ``'imag' | 'real'``
            Determines whether we perform real or imaginary time-evolution.
        E_offset : None | list of float
            Possible offset added to `H_bond` for real-time evolution.
        """
        U_param = dict(order=order, delta_t=delta_t, type_evo=type_evo, E_offset=E_offset)
        if type_evo == 'real':
            U_param['tau'] = delta_t
        elif type_evo == 'imag':
            U_param['tau'] = -1.j * delta_t
        else:
            raise ValueError("Invalid value for `type_evo`: " + repr(type_evo))
        if self._U_param == U_param and not self.force_prepare_evolve:
            logger.debug("Skip recalculation of U with same parameters as before")
            return  # nothing to do: U is cached
        self._U_param = U_param
        logger.info("Calculate U for %s", U_param)

        L = self.psi.L
        self._U = []
        for dt in self.suzuki_trotter_time_steps(order):
            U_bond = [self._calc_U_bond(i_bond, dt * delta_t, type_evo, E_offset) for i_bond in range(L)]
            self._U.append(U_bond)
        self.force_prepare_evolve = False

    def evolve(self, N_steps, dt):
        """Evolve by ``dt * N_steps``.

        Parameters
        ----------
        N_steps : int
            The number of steps for which the whole lattice should be updated.
        dt : float
            The time step; but really this was already used in :meth:`prepare_evolve`.

        Returns
        -------
        trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The error of the represented state which is introduced due to the truncation during
            this sequence of evolution steps.
        """
        if dt is not None:
            assert dt == self._U_param['delta_t']
        trunc_err = TruncationError()
        order = self._U_param['order']
        for U_idx_dt, odd in self.suzuki_trotter_decomposition(order, N_steps):
            trunc_err += self.evolve_step(U_idx_dt, odd)
        self.evolved_time = self.evolved_time + N_steps * self._U_param['tau']
        self.trunc_err = self.trunc_err + trunc_err  # not += : make a copy!
        # (this is done to avoid problems of users storing self.trunc_err after each `evolve`)
        return trunc_err

    def evolve_step(self, U_idx_dt, odd):
        """Updates either even *or* odd bonds in unit cell.

        Depending on the choice of p, this function updates all even (``E``, odd=False,0)
        **or** odd (``O``) (odd=True,1) bonds::

        |     - B0 - B1 - B2 - B3 - B4 - B5 - B6 -
        |       |    |    |    |    |    |    |
        |       |    |----|    |----|    |----|
        |       |    |  E |    |  E |    |  E |
        |       |    |----|    |----|    |----|
        |       |----|    |----|    |----|    |
        |       |  O |    |  O |    |  O |    |
        |       |----|    |----|    |----|    |

        Note that finite boundary conditions are taken care of by having ``Us[0] = None``.

        Parameters
        ----------
        U_idx_dt : int
            Time step index in ``self._U``,
            evolve with ``Us[i] = self.U[U_idx_dt][i]`` at bond ``(i-1,i)``.
        odd : bool/int
            Indication of whether to update even (``odd=False,0``) or even (``odd=True,1``) sites

        Returns
        -------
        trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The error of the represented state which is introduced due to the truncation
            during this sequence of update steps.
        """
        Us = self._U[U_idx_dt]
        trunc_err = TruncationError()
        for i_bond in np.arange(int(odd) % 2, self.psi.L, 2):
            if Us[i_bond] is None:
                continue  # handles finite vs. infinite boundary conditions
            self._update_index = (U_idx_dt, i_bond)
            trunc_err += self.update_bond(i_bond, Us[i_bond])
        self._update_index = None
        return trunc_err

    def update_bond(self, i, U_bond):
        """Updates the B matrices on a given bond.

        Function that updates the B matrices, the bond matrix s between and the
        bond dimension chi for bond i. The corresponding tensor networks look like this::

        |           --S--B1--B2--           --B1--B2--
        |                |   |                |   |
        |     theta:     U_bond        C:     U_bond
        |                |   |                |   |

        Parameters
        ----------
        i : int
            Bond index; we update the matrices at sites ``i-1, i``.
        U_bond : :class:`~tenpy.linalg.np_conserved.Array`
            The bond operator which we apply to the wave function.
            We expect labels ``'p0', 'p1', 'p0*', 'p1*'``.

        Returns
        -------
        trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The error of the represented state which is introduced by the truncation
            during this update step.
        """
        i0, i1 = i - 1, i
        logger.debug("Update sites (%d, %d)", i0, i1)
        # Construct the theta matrix
        C = self.psi.get_theta(i0, n=2, formL=0.)  # the two B without the S on the left
        C = npc.tensordot(U_bond, C, axes=(['p0*', 'p1*'], ['p0', 'p1']))  # apply U
        C.itranspose(['vL', 'p0', 'p1', 'vR'])
        theta = C.scale_axis(self.psi.get_SL(i0), 'vL')
        # now theta is the same as if we had done
        #   theta = self.psi.get_theta(i0, n=2)
        #   theta = npc.tensordot(U_bond, theta, axes=(['p0*', 'p1*'], ['p0', 'p1']))  # apply U
        # but also have C which is the same except the missing "S" on the left
        # so we don't have to apply inverses of S (see below)

        theta = theta.combine_legs([('vL', 'p0'), ('p1', 'vR')], qconj=[+1, -1])
        # Perform the SVD and truncate the wavefunction
        U, S, V, trunc_err, renormalize = svd_theta(theta,
                                                    self.trunc_params, [self.psi.get_B(i0, None).qtotal, None],
                                                    inner_labels=['vR', 'vL'])

        S *= renormalize  ## TODO: new change
        # Split tensor and update matrices
        B_R = V.split_legs(1).ireplace_label('p1', 'p')

        # In general, we want to do the following:
        #     U = U.iscale_axis(S, 'vR')
        #     B_L = U.split_legs(0).iscale_axis(self.psi.get_SL(i0)**-1, 'vL')
        #     B_L = B_L.ireplace_label('p0', 'p')
        # i.e. with SL = self.psi.get_SL(i0), we have ``B_L = SL**-1 U S``
        #
        # However, the inverse of SL is problematic, as it might contain very small singular
        # values.  Instead, we use ``C == SL**-1 theta == SL**-1 U S V``,
        # such that we obtain ``B_L = SL**-1 U S = SL**-1 U S V V^dagger = C V^dagger``
        # here, C is the same as theta, but without the `S` on the very left
        # (Note: this requires no inverse if the MPS is initially in 'B' canonical form)
        B_L = npc.tensordot(C.combine_legs(('p1', 'vR'), pipes=theta.legs[1]), V.conj(), axes=['(p1.vR)', '(p1*.vR*)'])
        B_L.ireplace_labels(['vL*', 'p0'], ['vR', 'p'])
        # B_L /= renormalize  # re-normalize to <psi|psi> = 1
        # self.psi.norm *= renormalize
        self.psi.set_SR(i0, S)
        self.psi.set_B(i0, B_L, form='B')
        self.psi.set_B(i1, B_R, form='B')
        self._trunc_err_bonds[i] = self._trunc_err_bonds[i] + trunc_err
        return trunc_err

    def update_imag(self, N_steps):
        """Perform an update suitable for imaginary time evolution.

        Instead of the even/odd brick structure used for ordinary TEBD,
        we 'sweep' from left to right and right to left, similar as DMRG.
        Thanks to that, we are actually able to preserve the canonical form.

        Parameters
        ----------
        N_steps : int
            The number of steps for which the whole lattice should be updated.

        Returns
        -------
        trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The error of the represented state which is introduced due to the truncation during
            this sequence of update steps.
        """
        trunc_err = TruncationError()
        order = self._U_param['order']
        # allow only second order evolution
        if order != 2 or not self.psi.finite:
            # Would lead to loss of canonical form. What about DMRG?
            raise NotImplementedError("Use DMRG instead...")
        U_idx_dt = 0  # always with dt=0.5
        assert (self.suzuki_trotter_time_steps(order)[U_idx_dt] == 0.5)
        assert (self.psi.finite)  # finite or segment bc
        Us = self._U[U_idx_dt]
        for _ in range(N_steps):
            # sweep right
            for i_bond in range(self.psi.L):
                if Us[i_bond] is None:
                    continue  # handles finite vs. infinite boundary conditions
                self._update_index = (U_idx_dt, i_bond)
                trunc_err += self.update_bond_imag(i_bond, Us[i_bond])
            # sweep left
            for i_bond in range(self.psi.L - 1, -1, -1):
                if Us[i_bond] is None:
                    continue  # handles finite vs. infinite boundary conditions
                self._update_index = (U_idx_dt, i_bond)
                trunc_err += self.update_bond_imag(i_bond, Us[i_bond])
        self._update_index = None
        self.evolved_time = self.evolved_time + N_steps * self._U_param['tau']
        self.trunc_err = self.trunc_err + trunc_err  # not += : make a copy!
        # (this is done to avoid problems of users storing self.trunc_err after each `update`)
        return trunc_err

    def update_bond_imag(self, i, U_bond):
        """Update a bond with a (possibly non-unitary) `U_bond`.

        Similar as :meth:`update_bond`; but after the SVD just keep the `A, S, B` canonical form.
        In that way, one can sweep left or right without using old singular values,
        thus preserving the canonical form during imaginary time evolution.

        Parameters
        ----------
        i : int
            Bond index; we update the matrices at sites ``i-1, i``.
        U_bond : :class:`~tenpy.linalg.np_conserved.Array`
            The bond operator which we apply to the wave function.
            We expect labels ``'p0', 'p1', 'p0*', 'p1*'``.

        Returns
        -------
        trunc_err : :class:`~tenpy.algorithms.truncation.TruncationError`
            The error of the represented state which is introduced by the truncation
            during this update step.
        """
        i0, i1 = i - 1, i
        logger.debug("Update sites (%d, %d)", i0, i1)
        # Construct the theta matrix
        theta = self.psi.get_theta(i0, n=2)  # 'vL', 'vR', 'p0', 'p1'
        theta = npc.tensordot(U_bond, theta, axes=(['p0*', 'p1*'], ['p0', 'p1']))
        theta = theta.combine_legs([('vL', 'p0'), ('vR', 'p1')], qconj=[+1, -1])
        # Perform the SVD and truncate the wavefunction
        U, S, V, trunc_err, renormalize = svd_theta(theta, self.trunc_params, inner_labels=['vR', 'vL'])
        S *= renormalize  ## TODO: new change
        # self.psi.norm *= renormalize
        # Split legs and update matrices
        B_R = V.split_legs(1).ireplace_label('p1', 'p')
        A_L = U.split_legs(0).ireplace_label('p0', 'p')
        self.psi.set_SR(i0, S)
        self.psi.set_B(i0, A_L, form='A')
        self.psi.set_B(i1, B_R, form='B')
        self._trunc_err_bonds[i] = self._trunc_err_bonds[i] + trunc_err
        return trunc_err

    def _calc_U_bond(self, i_bond, dt, type_evo, E_offset):
        """Calculate exponential of a bond Hamiltonian.

        * ``U_bond = exp(-i dt (H_bond-E_offset_bond))`` for ``type_evo='real'``, or
        * ``U_bond = exp(- dt H_bond)`` for ``type_evo='imag'``.
        """
        h = self.model.H_bond[i_bond]
        if h is None:
            return None  # don't calculate exp(i H t), if `H` is None
        H2 = h.combine_legs([('p0', 'p1'), ('p0*', 'p1*')], qconj=[+1, -1])
        if type_evo == 'imag':
            H2 = (-dt) * H2
        elif type_evo == 'real':
            if E_offset is not None:
                H2 = H2 - npc.diag(E_offset[i_bond], H2.legs[0])
            H2 = (-1.j * dt) * H2
        else:
            raise ValueError("Expect either 'real' or 'imag'inary time, got " + repr(type_evo))
        U = npc.expm(H2)
        assert (tuple(U.get_leg_labels()) == ('(p0.p1)', '(p0*.p1*)'))
        return U.split_legs()
