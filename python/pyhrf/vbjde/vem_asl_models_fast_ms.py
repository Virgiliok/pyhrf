# -*- coding: utf-8 -*-
"""VEM BOLD Constrained

File that contains function for BOLD data analysis with positivity
and l2-norm=1 constraints.

It imports functions from vem_tools.py in pyhrf/vbjde
"""

import time
import copy
import logging
import os

import os.path as op
import numpy as np

import pyhrf
import pyhrf.vbjde.vem_tools as vt

from pyhrf.boldsynth.hrf import getCanoHRF, genGaussianSmoothHRF
from pyhrf.sandbox.physio_params import (PHY_PARAMS_KHALIDOV11,
                                         linear_rf_operator,
                                         create_physio_brf,
                                         create_physio_prf)

import matplotlib
import matplotlib.pyplot as plt
try:
    os.environ["DISPLAY"]
except KeyError:
    matplotlib.use("Agg")
    plt.switch_backend("Agg")
else:
    try:
        matplotlib.use("Qt4Agg")
        plt.switch_backend("Qt4Agg")
    except ImportError:
        pass


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

eps = 1e-6

#@profile
def Main_vbjde_physio(graph, Y, Onsets, durations, Thrf, K, TR, beta, dt,
                      scale=1, estimateSigmaH=True, estimateSigmaG=True,
                      sigmaH=0.05, sigmaG=0.05, gamma_h=0, gamma_g=0,
                      NitMax=-1, NitMin=1, estimateBeta=True, PLOT=False,
                      contrasts=[], computeContrast=False,
                      idx_first_tag=0, simulation=None, sigmaMu=None,
                      estimateH=True, estimateG=True, estimateA=True,
                      estimateC=True, estimateZ=True, estimateNoise=True,
                      estimateMP=True, estimateLA=True, use_hyperprior=False,
                      positivity=False, constraint=False,
                      phy_params=PHY_PARAMS_KHALIDOV11, prior='omega', zc=False):

    logger.info("EM for ASL!")
    np.random.seed(6537540)
    logger.info("data shape: ")
    logger.info(Y.shape)

    Thresh = 1e-5
    D, M = np.int(np.ceil(Thrf / dt)) + 1, len(Onsets)
    #D, M = np.int(np.ceil(Thrf / dt)), len(Onsets)
    n_sess, N, J = Y.shape[0], Y.shape[1], Y.shape[2]

    Crit_AH, Crit_CG, cTime, rerror, FE = 1, 1, [], [], []
    EP, EPlh, Ent = [],[],[]
    Crit_H, Crit_G, Crit_Z, Crit_A, Crit_C = 1, 1, 1, 1, 1
    cAH, cCG, AH1, CG1 = [], [], [], []
    cA, cC, cH, cG, cZ = [], [], [], [], []
    h_norm, g_norm = [], []
    SUM_q_Z = [[] for m in xrange(M)]
    mua1 = [[] for m in xrange(M)]
    muc1 = [[] for m in xrange(M)]
    sigmaH = sigmaH * J / 100
    print sigmaH
    gamma_h = gamma_h * 100 / J
    print gamma_h

    # Beta data
    MaxItGrad = 200
    gradientStep = 0.005
    gamma = 7.5
    print 'gamma = ', gamma
    print 'voxels = ', J
    maxNeighbours, neighboursIndexes = vt.create_neighbours(graph, J)
    print 'graph.shape = ', graph.shape

    # Control-tag
    w = np.ones((N))
    w[idx_first_tag + 1::2] = -1
    W = np.diag(w)

    # Conditions
    print 'Onsets: ', Onsets
    print 'durations = ', durations
    print 'creating conditions...'
    X, XX, condition_names = vt.create_conditions_block_ms(Onsets, durations,
                                                    M, N, D, n_sess, TR, dt)
    # Covariance matrix
    #R = vt.covariance_matrix(2, D, dt)
    _, R_inv = genGaussianSmoothHRF(zc, D, dt, 1., 2)
    R = np.linalg.inv(R_inv)
    if zc:
        XX = XX[:, :, :, 1:-1]    # XX shape (S, M, N, D)
        D = D - 2
    AH1, CG1 = np.zeros((J, M, D)), np.zeros((J, M, D))

    print 'HRF length = ', D
    print 'Condition number = ', M
    print 'Number of scans = ', N
    print 'Number of voxels = ', J
    print 'Number of sessions = ', n_sess
    print 'XX.shape = ', XX.shape

    # Noise matrix
    Gamma = np.identity(N)
    # Noise initialization
    sigma_eps = np.ones((n_sess, J))
    # Labels
    logger.info("Labels are initialized by setting active probabilities "
                "to ones ...")
    q_Z = np.ones((M, K, J), dtype=np.float64) / 2.
    #q_Z = np.zeros((M, K, J), dtype=np.float64)
    #q_Z[:, 1, :] = 1
    q_Z1 = copy.deepcopy(q_Z)
    Z_tilde = copy.deepcopy(q_Z)

    # H and G
    TT, m_h = getCanoHRF(Thrf, dt)
    H = np.array(m_h[:D]).astype(np.float64)
    H /= np.linalg.norm(H)
    G = copy.deepcopy(H)
    Hb = create_physio_brf(phy_params, response_dt=dt, response_duration=Thrf)
    Hb /= np.linalg.norm(Hb)
    Gb = create_physio_prf(phy_params, response_dt=dt, response_duration=Thrf)
    Gb /= np.linalg.norm(Gb)
    if prior=='balloon':
        H = Hb.copy()
        G = Gb.copy()
    Mu = Hb.copy()
    H1 = copy.deepcopy(H)
    Sigma_H = np.zeros((D, D), dtype=np.float64)
    G1 = copy.deepcopy(G)
    Sigma_G = copy.deepcopy(Sigma_H)
    normOh = False
    normg = False
    if prior=='hierarchical' or prior=='omega':
        Omega = linear_rf_operator(len(H), phy_params, dt, calculating_brf=False)
    if prior=='omega':
        Omega0 = Omega.copy()
        OmegaH = np.dot(Omega, H)
        G = np.dot(Omega, H)
        if normOh or normg:
            Omega /= np.linalg.norm(OmegaH)
            OmegaH /=np.linalg.norm(OmegaH)
            G /= np.linalg.norm(G)

    # Initialize model parameters
    Beta = beta * np.ones((M), dtype=np.float64)
    n_drift = 4
    P = np.zeros((n_sess, N, n_drift+1), dtype=np.float64)
    L = np.zeros((n_drift+1, J, n_sess), dtype=np.float64)
    WP = np.zeros((n_sess, N, n_drift+2), dtype=np.float64)
    AL = np.zeros((n_drift+2, J, n_sess), dtype=np.float64)
    alpha = np.zeros((J), dtype=np.float64)
    for s in xrange(0, n_sess):
        P[s, :, :] = vt.PolyMat(N, n_drift, TR)
        L[:, :, s] = vt.polyFit(Y[s, :, :], TR, n_drift, P[s, :, :])
        WP[s, :, :] = np.append(w[:, np.newaxis], P[s, :, :], axis=1)
        AL[:, :, s] = np.append(alpha[np.newaxis, :], L[:, :, s], axis=0)
    PL = np.einsum('ijk,kli->ijl', WP, AL)
    y_tilde = Y - PL

    # Parameters Gaussian mixtures
    mu_Ma = np.append(np.zeros((M, 1)), np.ones((M, 1)), axis=1).astype(np.float64)
    mu_Mc = mu_Ma.copy()
    sigma_Ma = np.ones((M, K), dtype=np.float64) * 0.3
    sigma_Mc = sigma_Ma.copy()

    # Params RLs
    m_A = np.zeros((n_sess, J, M), dtype=np.float64)
    for s in xrange(0, n_sess):
        for j in xrange(0, J):
            m_A[s, j, :] = (np.random.normal(mu_Ma, np.sqrt(sigma_Ma)) * q_Z[:, :, j]).sum(axis=1).T
    m_A1 = m_A.copy()
    Sigma_A = np.ones((M, M, J, n_sess)) * np.identity(M)[:, :, np.newaxis, np.newaxis]
    m_C = m_A.copy()
    m_C1 = m_C.copy()
    Sigma_C = Sigma_A.copy()

    # Precomputations
    print 'W shape is ', W.shape
    WX = W.dot(XX).transpose(1, 2, 0, 3)                                       # shape (S, M, N, D)
    Gamma_X = np.zeros((N, n_sess, M, D), dtype=np.float64)                    # shape (N, S, M, D)
    X_Gamma_X = np.zeros((D, M, n_sess, M, D), dtype=np.float64)               # shape (D, M, S, M, D)
    Gamma_WX = np.zeros((N, n_sess, M, D), dtype=np.float64)                   # shape (N, S, M, D)
    XW_Gamma_WX = np.zeros((D, M, n_sess, M, D), dtype=np.float64)             # shape (D, M, S, M, D)
    Gamma_WP = np.zeros((N, n_sess, n_drift+2), dtype=np.float64)              # shape (N, S, M, D)
    WP_Gamma_WP = np.zeros((n_sess, n_drift+2, n_drift+2), dtype=np.float64)   # shape (D, M, S, M, D)
    for s in xrange(0, n_sess):
        Gamma_X[:, s, :, :] = np.tensordot(Gamma, XX[s, :, :, :], axes=(1, 1))
        X_Gamma_X[:, :, s, :, :] = np.tensordot(XX[s, :, :, :].T, Gamma_X[:, s, :, :], axes=(1, 0))
        Gamma_WX[:, s, :, :] = np.tensordot(Gamma, WX[s, :, :, :], axes=(1, 1))
        XW_Gamma_WX[:, :, s, :, :] = np.tensordot(WX[s, :, :, :].T, Gamma_WX[:, s, :, :], axes=(1, 0))
        Gamma_WP[:, s, :] = Gamma.dot(WP[s, :, :])                             # (N, n_drift)
        WP_Gamma_WP[s, :, :] = WP[s, :, :].T.dot(Gamma_WP[:, s, :])            # (n_drift, n_drift)
    sigma_eps_m = np.maximum(sigma_eps, eps)                                   # (n_sess, J)
    cov_noise = sigma_eps_m[:, :, np.newaxis, np.newaxis]                      # (n_sess, J, 1, 1)


    ###########################################################################
    #############################################             VBJDE

    t1 = time.time()
    ni = 0

    #while ((ni < NitMin + 1) or (((Crit_AH > Thresh) or (Crit_CG > Thresh)) \
    #        and (ni < NitMax))):
    #while ((ni < NitMin + 1) or (((Crit_AH > Thresh)) \
    #        and (ni < NitMax))):
    while ((ni < NitMin + 1) or (((Crit_FE > Thresh * np.ones_like(Crit_FE)).any()) \
            and (ni < NitMax))):

        logger.info("-------- Iteration n° " + str(ni + 1) + " --------")

        if PLOT and ni >= 0:  # Plotting HRF and PRF
            logger.info("Plotting HRF and PRF for current iteration")
            vt.plot_response_functions_it(ni, NitMin, M, H, G)


        # Managing types of prior
        priorH_cov_term = np.zeros_like(R_inv)
        priorG_cov_term = np.zeros_like(R_inv)
        matrix_covH = R_inv.copy()
        matrix_covG = R_inv.copy()
        if prior=='balloon':
            logger.info("   prior balloon")
            #matrix_covH = np.eye(R_inv.shape[0], R_inv.shape[1])
            #matrix_covG = np.eye(R_inv.shape[0], R_inv.shape[1])
            priorH_mean_term = np.dot(matrix_covH / sigmaH, Hb)
            priorG_mean_term = np.dot(matrix_covG / sigmaG, Gb)
        elif prior=='omega':
            logger.info("   prior omega")
            #matrix_covG = np.eye(R_inv.shape[0], R_inv.shape[1])
            priorH_mean_term = np.dot(np.dot(Omega.T, matrix_covG / sigmaG), G)
            priorH_cov_term = np.dot(np.dot(Omega.T, matrix_covG / sigmaG), Omega)
            priorG_mean_term = np.dot(matrix_covG / sigmaG, OmegaH)
        elif prior=='hierarchical':
            logger.info("   prior hierarchical")
            matrix_covH = np.eye(R_inv.shape[0], R_inv.shape[1])
            matrix_covG = np.eye(R_inv.shape[0], R_inv.shape[1])
            priorH_mean_term = Mu / sigmaH
            priorG_mean_term = np.dot(Omega, Mu / sigmaG)
        else:
            logger.info("   NO prior")
            priorH_mean_term = np.zeros_like(H)
            priorG_mean_term = np.zeros_like(G)



        #####################
        # EXPECTATION
        #####################


        # HRF H
        if estimateH:
            logger.info("E H step ...")
            Ht, Sigma_H = vt.expectation_H_ms(Sigma_A, m_A, m_C, G, XX, W, Gamma,
                                            Gamma_X, X_Gamma_X, J, y_tilde,
                                            cov_noise, matrix_covH, sigmaH,
                                            priorH_mean_term, priorH_cov_term, N, M, D, n_sess)

            if constraint:
                if not np.linalg.norm(Ht)==1:
                    logger.info("   constraint l2-norm = 1")
                    H = vt.constraint_norm1_b(Ht, Sigma_H)
                    #H = Ht / np.linalg.norm(Ht)
                else:
                    logger.info("   l2-norm already 1!!!!!")
                    H = Ht.copy()
                Sigma_H = np.zeros_like(Sigma_H)
            else:
                H = Ht.copy()
                h_norm = np.append(h_norm, np.linalg.norm(H))
                print 'h_norm = ', h_norm

            Crit_H = (np.linalg.norm(H - H1) / np.linalg.norm(H1)) ** 2
            cH += [Crit_H]
            H1[:] = H[:]
            if prior=='omega':
                OmegaH = np.dot(Omega0, H)
                Omega = Omega0
                if normOh:
                    Omega /= np.linalg.norm(OmegaH)
                    OmegaH /= np.linalg.norm(OmegaH)


        # A
        if estimateA:
            logger.info("E A step ...")
            m_A, Sigma_A = vt.expectation_A_ms(m_A, Sigma_A, H, G, m_C, W, XX,
                                             Gamma, Gamma_X, q_Z,
                                             mu_Ma, sigma_Ma, J, y_tilde,
                                             Sigma_H, sigma_eps_m, N, M, D, n_sess)

            cA += [(np.linalg.norm(m_A - m_A1) / np.linalg.norm(m_A1)) ** 2]
            m_A1[:, :, :] = m_A[:, :, :]


        # PRF G
        if estimateG:
            logger.info("E G step ...")
            Gt, Sigma_G = vt.expectation_G_ms(Sigma_C, m_C, m_A, H, XX, W, WX, Gamma,
                                            Gamma_WX, XW_Gamma_WX, J, y_tilde,
                                            cov_noise, matrix_covG, sigmaG,
                                            priorG_mean_term, priorG_cov_term, N, M, D, n_sess)

            if constraint and normg:
                if not np.linalg.norm(Gt)==1:
                    logger.info("   constraint l2-norm = 1")
                    G = vt.constraint_norm1_b(Gt, Sigma_G, positivity=positivity)
                    #G = Gt / np.linalg.norm(Gt)
                else:
                    logger.info("   l2-norm already 1!!!!!")
                    G = Gt.copy()
                Sigma_G = np.zeros_like(Sigma_G)
            else:
                G = Gt.copy()
                g_norm = np.append(g_norm, np.linalg.norm(G))
                print 'g_norm = ', g_norm
            cG += [(np.linalg.norm(G - G1) / np.linalg.norm(G1)) ** 2]
            G1[:] = G[:]

        # C
        if estimateC:
            logger.info("E C step ...")
            m_C, Sigma_C = vt.expectation_C_ms(m_C, Sigma_C, G, H, m_A, W, XX, Gamma, Gamma_X, q_Z,
                                             mu_Mc, sigma_Mc, J, y_tilde,
                                             Sigma_G, sigma_eps_m, N, M, D, n_sess)

            cC += [(np.linalg.norm(m_C - m_C1) / np.linalg.norm(m_C1)) ** 2]
            m_C1[:, :] = m_C[:, :]


        # Q labels
        if estimateZ:
            logger.info("E Q step ...")
            q_Z, Z_tilde = vt.expectation_Q_ms(Sigma_A, m_A, Sigma_C, m_C,
                                            sigma_Ma, mu_Ma, sigma_Mc, mu_Mc,
                                            Beta, Z_tilde, q_Z, neighboursIndexes, graph, M, J, K, n_sess)

            if 0:
                import matplotlib.pyplot as plt
                plt.close('all')
                fig = plt.figure(1)
                for m in xrange(M):
                    ax = fig.add_subplot(2, M, m + 1)
                    im = ax.matshow(m_A[:, :, m].mean(0).reshape(20, 20))
                    plt.colorbar(im, ax=ax)
                    ax = fig.add_subplot(2, M, m + 3)
                    im = ax.matshow(q_Z[m, 1, :].reshape(20, 20))
                    plt.colorbar(im, ax=ax)
                fig = plt.figure(2)
                for m in xrange(M):
                    for s in xrange(n_sess):
                        ax = fig.add_subplot(M, n_sess, n_sess * m + s + 1)
                        im = ax.matshow(m_A[s, :, m].reshape(20, 20))
                        plt.colorbar(im, ax=ax)
                plt.show()

            cZ += [(np.linalg.norm(q_Z - q_Z1) / (np.linalg.norm(q_Z1) + eps)) ** 2]
            q_Z1 = q_Z

        if ni > 0:
            free_energyE = 0
            for s in xrange(n_sess):
                free_energyE += vt.Compute_FreeEnergy(y_tilde[s, :, :], m_A[s, :, :], Sigma_A[:, :, :, s],
                                             mu_Ma, sigma_Ma, H, Sigma_H, AuxH, R, R_inv, sigmaH, sigmaG,
                                             m_C[s, :, :], Sigma_C[:, :, :, s], mu_Mc, sigma_Mc, G, Sigma_G,
                                             AuxG, q_Z, neighboursIndexes, Beta, Gamma,
                                             gamma, gamma_h, gamma_g, sigma_eps[s, :], XX[s, :, :, :],
                                             W, J, D, M, N, K, use_hyperprior, Gamma_X[:, s, :, :],
                                             Gamma_WX[:, s, :, :], bold=True, S=n_sess)
            if free_energyE < free_energy:
                logger.info("free energy has decreased after E step from %f to %f", free_energy, free_energyE)


        # crit. AH and CG
        logger.info("crit. AH and CG")
        AH = m_A[:, :, :, np.newaxis] * H[np.newaxis, np.newaxis, :]
        CG = m_C[:, :, :, np.newaxis] * G[np.newaxis, np.newaxis, :]

        Crit_AH = (np.linalg.norm(AH - AH1) / (np.linalg.norm(AH1) + eps)) ** 2
        cAH += [Crit_AH]
        AH1 = AH.copy()
        Crit_CG = (np.linalg.norm(CG - CG1) / (np.linalg.norm(CG1) + eps)) ** 2
        cCG += [Crit_CG]
        CG1 = CG.copy()
        logger.info("Crit_AH = " + str(Crit_AH))
        logger.info("Crit_CG = " + str(Crit_CG))


        #####################
        # MAXIMIZATION
        #####################

        if prior=='balloon':
            logger.info("   prior balloon")
            AuxH = H - Hb
            AuxG = G - Gb
        elif prior=='omega':
            logger.info("   prior omega")
            AuxH = H.copy()
            AuxG = G - np.dot(Omega, H) #/np.linalg.norm(np.dot(Omega, H))
        elif prior=='hierarchical':
            logger.info("   prior hierarchical")
            AuxH = H - Mu
            AuxG = G - np.dot(Omega, Mu)
        else:
            logger.info("   NO prior")
            AuxH = H.copy()
            AuxG = G.copy()

        # Variance HRF: sigmaH
        if estimateSigmaH:
            logger.info("M sigma_H step ...")
            sigmaH = vt.maximization_sigma_asl(D, Sigma_H, matrix_covH, AuxH, use_hyperprior, gamma_h)
            logger.info('sigmaH = ' + str(sigmaH))

        if ni > 0:
            free_energyVh = 0
            for s in xrange(n_sess):
                free_energyVh += vt.Compute_FreeEnergy(y_tilde[s, :, :], m_A[s, :, :], Sigma_A[:, :, :, s], mu_Ma, sigma_Ma,
                                             H, Sigma_H, AuxH, R, R_inv, sigmaH, sigmaG,
                                             m_C[s, :, :], Sigma_C[:, :, :, s], mu_Mc, sigma_Mc, G, Sigma_G,
                                             AuxG, q_Z, neighboursIndexes, Beta, Gamma,
                                             gamma, gamma_h, gamma_g, sigma_eps[s, :], XX[s, :, :, :], W,
                                             J, D, M, N, K, use_hyperprior, Gamma_X[:, s, :, :], Gamma_WX[:, s, :, :], bold=True, S=n_sess)
            if free_energyVh < free_energyE:
                logger.info("free energy has decreased after v_h computation from %f to %f", free_energyE, free_energyVh)

         # Variance PRF: sigmaG
        if estimateSigmaG:
            logger.info("M sigma_G step ...")
            sigmaG = vt.maximization_sigma_asl(D, Sigma_G, matrix_covG, AuxG, use_hyperprior, gamma_g)
            logger.info('sigmaG = ' + str(sigmaG))

        # Mu: True HRF in the hierarchical prior case
        if prior=='hierarchical':
            logger.info("M sigma_G step ...")
            Mu = vt.maximization_Mu(H, G, matrix_covH, matrix_covG,
                                     sigmaH, sigmaG, sigmaMu, Omega, R_inv)
            logger.info('sigmaG = ' + str(sigmaG))

        # (mu,sigma)
        if estimateMP:
            logger.info("M (mu,sigma) a and c step ...")
            mu_Ma, sigma_Ma = vt.maximization_mu_sigma_ms(q_Z, m_A, Sigma_A, M, J, n_sess, K)
            mu_Mc, sigma_Mc = vt.maximization_mu_sigma_ms(q_Z, m_C, Sigma_C, M, J, n_sess, K)

        if ni > 0:
            free_energyMP = 0
            for s in xrange(n_sess):
                free_energyMP += vt.Compute_FreeEnergy(y_tilde[s, :, :], m_A[s, :, :], Sigma_A[:, :, :, s], mu_Ma, sigma_Ma,
                                             H, Sigma_H, AuxH, R, R_inv, sigmaH, sigmaG,
                                             m_C[s, :, :], Sigma_C[:, :, :, s], mu_Mc, sigma_Mc, G, Sigma_G,
                                             AuxG, q_Z, neighboursIndexes, Beta, Gamma,
                                             gamma, gamma_h, gamma_g, sigma_eps[s, :], XX[s, :, :, :], W,
                                             J, D, M, N, K, use_hyperprior, Gamma_X[:, s, :, :], Gamma_WX[:, s, :, :], bold=True, S=n_sess)
            if free_energyMP < free_energyVh:
                logger.info("free energy has decreased after GMM parameters computation from %f to %f", free_energyVh, free_energyMP)


        # Drift L, alpha
        if estimateLA:
            logger.info("M L, alpha step ...")
            for s in xrange(n_sess):
                AL[:, :, s] = vt.maximization_LA_asl(Y[s, :, :], m_A[s, :, :], m_C[s, :, :], XX[s, :, :, :],
                                                     WP[s, :, :], W, WP_Gamma_WP[s, :, :], H, G, Gamma)
            PL = np.einsum('ijk,kli->ijl', WP, AL)
            y_tilde = Y - PL

        if ni > 0:
            free_energyLA = 0
            for s in xrange(n_sess):
                free_energyLA += vt.Compute_FreeEnergy(y_tilde[s, :, :], m_A[s, :, :], Sigma_A[:, :, :, s], mu_Ma, sigma_Ma,
                                                 H, Sigma_H, AuxH, R, R_inv, sigmaH, sigmaG,
                                                 m_C[s, :, :], Sigma_C[:, :, :, s], mu_Mc, sigma_Mc, G, Sigma_G,
                                                 AuxG, q_Z, neighboursIndexes, Beta, Gamma,
                                                 gamma, gamma_h, gamma_g, sigma_eps[s, :], XX[s, :, :, :], W,
                                                 J, D, M, N, K, use_hyperprior, Gamma_X[:, s, :, :], Gamma_WX[:, s, :, :], bold=True, S=n_sess)
            if free_energyLA < free_energyMP:
                logger.info("free energy has decreased after drifts computation from %f to %f", free_energyMP, free_energyLA)


        # Beta
        if estimateBeta:
            logger.info("M beta step ...")
            """Qtilde = np.concatenate((Z_tilde, np.zeros((M, K, 1), dtype=Z_tilde.dtype)), axis=2)
            Qtilde_sumneighbour = Qtilde[:, :, neighboursIndexes].sum(axis=3)
            Beta = vt.maximization_beta_m2(Beta.copy(), q_Z, Qtilde_sumneighbour,
                                             Qtilde, neighboursIndexes, maxNeighbours,
                                             gamma, MaxItGrad, gradientStep)
            logger.info(Beta)
            """
            Qtilde = np.concatenate((Z_tilde, np.zeros((M, K, 1), dtype=Z_tilde.dtype)), axis=2)
            Qtilde_sumneighbour = Qtilde[:, :, neighboursIndexes].sum(axis=3)
            for m in xrange(0, M):
                Beta[m] = vt.maximization_beta_m2_scipy_asl(Beta[m].copy(), q_Z[m, :, :], Qtilde_sumneighbour[m, :, :],
                                                   Qtilde[m, :, :], neighboursIndexes, maxNeighbours,
                                                   gamma, MaxItGrad, gradientStep)
            logger.info(Beta)
        if ni > 0:
            free_energyB = 0
            for s in xrange(n_sess):
                free_energyB += vt.Compute_FreeEnergy(y_tilde[s, :, :], m_A[s, :, :], Sigma_A[:, :, :, s], mu_Ma, sigma_Ma,
                                             H, Sigma_H, AuxH, R, R_inv, sigmaH, sigmaG,
                                             m_C[s, :, :], Sigma_C[:, :, :, s], mu_Mc, sigma_Mc, G, Sigma_G,
                                             AuxG, q_Z, neighboursIndexes, Beta, Gamma,
                                             gamma, gamma_h, gamma_g, sigma_eps[s, :], XX[s, :, :, :], W,
                                             J, D, M, N, K, use_hyperprior, Gamma_X[:, s, :, :], Gamma_WX[:, s, :, :], bold=True, S=n_sess)
            if free_energyB < free_energyLA:
                logger.info("free energy has decreased after Beta computation from %f to %f", \
                                free_energyLA, free_energyB)
        if 0 and ni < 5:
            plt.close('all')
            for m in xrange(0, M):
                range_b = np.arange(-10., 20., 0.1)
                beta_plotting = np.zeros_like(range_b)
                grad_plotting = np.zeros_like(range_b)
                for ib, b in enumerate(range_b):
                    beta_plotting[ib] = vt.fun(b, q_Z[m, :, :], Qtilde_sumneighbour[m, :, :],
                                                          neighboursIndexes, gamma)
                    grad_plotting[ib] = vt.grad_fun(b, q_Z[m, :, :], Qtilde_sumneighbour[m, :, :],
                                                     neighboursIndexes, gamma)
                #print beta_plotting
                plt.figure(1)
                plt.hold('on')
                plt.plot(range_b, beta_plotting)
                plt.figure(2)
                plt.hold('on')
                plt.plot(range_b, grad_plotting)
            plt.show()


        # Sigma noise
        if estimateNoise:
            logger.info("M sigma noise step ...")
            for s in xrange(n_sess):
                sigma_eps[s, :] = vt.maximization_sigma_noise_asl(XX[s, :, :, :], m_A[s, :, :], Sigma_A[:, :, :, s], H, m_C[s, :, :], Sigma_C[:, :, :, s], \
                                                    G, Sigma_H, Sigma_G, W, y_tilde[s, :, :], Gamma, \
                                                    Gamma_X[:, s, :, :], Gamma_WX[:, s, :, :], N)

        if PLOT:
            for m in xrange(M):
                SUM_q_Z[m] += [q_Z[m, 1, :].sum()]
                mua1[m] += [mu_Ma[m, 1]]

        free_energy = 0
        for s in xrange(n_sess):
            if s==n_sess-1:
                plotFE = True
            else:
                plotFE = False
            free_energy += vt.Compute_FreeEnergy(y_tilde[s, :, :], m_A[s, :, :], Sigma_A[:, :, :, s], mu_Ma, sigma_Ma,
                                             H, Sigma_H, AuxH, R, R_inv, sigmaH, sigmaG,
                                             m_C[s, :, :], Sigma_C[:, :, :, s], mu_Mc, sigma_Mc, G, Sigma_G,
                                             AuxG, q_Z, neighboursIndexes, Beta, Gamma,
                                             gamma, gamma_h, gamma_g, sigma_eps[s, :], XX[s, :, :, :], W,
                                             J, D, M, N, K, use_hyperprior, Gamma_X[:, s, :, :], Gamma_WX[:, s, :, :],
                                             plot=plotFE, bold=True, S=n_sess)
        if ni > 0:
            if free_energy < free_energyB:
                logger.info("free energy has decreased after Noise computation from %f to %f", free_energyB, free_energy)

        if ni > 0:
            if free_energy < FE[-1]:
                logger.info("WARNING! free energy has decreased in this iteration from %f to %f", FE[-1], free_energy)

        FE += [free_energy]

        if ni > 5:
            #Crit_FE = np.abs((FE[-1] - FE[-2]) / FE[-2])
            FE0 = np.array(FE)
            Crit_FE = np.abs((FE0[-5:] - FE0[-6:-1]) / FE0[-6:-1])
            print Crit_FE
            print (Crit_FE > Thresh * np.ones_like(Crit_FE)).any()
        else:
            Crit_FE = 100

        ni += 1
        cTime += [time.time() - t1]

        logger.info("Computing reconstruction error")
        StimulusInducedSignal = vt.computeFit_asl(H, m_A[s, :, :], G, m_C[s, :, :], W, XX[s, :, :, :])
        rerror = np.append(rerror, \
                           np.mean(((Y[s, :, :] - StimulusInducedSignal) ** 2).sum(axis=0)) \
                           / np.mean((Y[s, :, :] ** 2).sum(axis=0)))

    CompTime = time.time() - t1


    # Normalize if not done already
    if not constraint: # or not normg:
        logger.info("l2-norm of H and G to 1 if not constraint")
        Hnorm = np.linalg.norm(H)
        H /= Hnorm
        Sigma_H /= Hnorm**2
        m_A *= Hnorm
        Gnorm = np.linalg.norm(G)
        G /= Gnorm
        Sigma_G /= Gnorm**2
        m_C *= Gnorm

    if zc:
        H = np.concatenate(([0], H, [0]))

    ## Compute contrast maps and variance
    if computeContrast and len(contrasts) > 0:
        logger.info("Computing contrasts ... ")
        CONTRAST_A, CONTRASTVAR_A, \
        CONTRAST_C, CONTRASTVAR_C = vt.compute_contrasts(condition_names,
                                                         contrasts, m_A[s, :, :], m_C[s, :, :],
                                                         Sigma_A[:, :, :, s], Sigma_C[:, :, :, s], M, J)
    else:
        CONTRAST_A, CONTRASTVAR_A, CONTRAST_C, CONTRASTVAR_C = 0, 0, 0, 0


    ###########################################################################
    ##########################################    PLOTS and SNR computation

    if PLOT:
        logger.info("plotting...")
        print 'FE = ', FE
        vt.plot_convergence(ni, M, cA, cC, cH, cG, cAH, cCG, SUM_q_Z, mua1, muc1, FE)

    logger.info("Nb iterations to reach criterion: %d",  ni)
    logger.info("Computational time = %s min %s s",
                str(np.int(CompTime // 60)), str(np.int(CompTime % 60)))
    logger.info("Iteration time = %s min %s s",
                str(np.int((CompTime // ni) // 60)), str(np.int((CompTime / ni) % 60)))

    logger.info("perfusion baseline mean = %f", np.mean(AL[0, :, s]))
    logger.info("perfusion baseline var = %f", np.var(AL[0, :, s]))
    logger.info("drifts mean = %f", np.mean(AL[1:, :, s]))
    logger.info("drifts var = %f", np.var(AL[1:, :, s]))
    logger.info("noise mean = %f", np.mean(sigma_eps[s, :]))
    logger.info("noise var = %f", np.var(sigma_eps[s, :]))

    SNR10 = 20 * (np.log10(np.linalg.norm(Y[s, :, :]) / \
                np.linalg.norm(Y[s, :, :] - StimulusInducedSignal - PL[s, :, :])))
    logger.info("SNR = %d",  SNR10)

    return ni, m_A.mean(0), H, m_C.mean(0), G, Z_tilde, sigma_eps[s, :], \
           mu_Ma, sigma_Ma, mu_Mc, sigma_Mc, Beta, AL[:, :, s], PL[s, :, :], \
           np.zeros_like(AL[0, :, s]), Sigma_A[:, :, :, s], Sigma_C[:, :, :, s], Sigma_H, Sigma_G, rerror, \
           CONTRAST_A, CONTRASTVAR_A, CONTRAST_C, CONTRASTVAR_C, \
           cA[:], cH[2:], cC[2:], cG[2:], cZ[2:], cAH[2:], cCG[2:], \
           cTime, FE

