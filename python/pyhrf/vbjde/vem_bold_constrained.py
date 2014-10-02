# -*- coding: utf-8 -*-
"""VEM BOLD Constrained

File that contains function for BOLD data analysis with positivity 
and l2-norm=1 constraints. 

It imports functions from vem_tools.py in pyhrf/vbjde
"""

import os.path as op
import numpy as np
import time
import UtilsC
import pyhrf.verbose 
from pyhrf.tools.io import read_volume 
from pyhrf.boldsynth.hrf import getCanoHRF
from pyhrf.ndarray import xndarray
import vem_tools as vt

try:
    from collections import OrderedDict
except ImportError:
    from pyhrf.tools.backports import OrderedDict


def Main_vbjde_Extension_constrained(graph,Y,Onsets,Thrf,K,TR,beta,dt,scale=1,estimateSigmaH=True,sigmaH = 0.05,NitMax = -1,NitMin = 1,estimateBeta=True,PLOT=False,contrasts=[],computeContrast=False,gamma_h=0,estimateHRF=True,TrueHrfFlag=False,HrfFilename='hrf.nii',estimateLabels=True,LabelsFilename='labels.nii',MFapprox=False,InitVar=0.5,InitMean=2.0,MiniVEMFlag=False,NbItMiniVem=5):    
    # VBJDE Function for BOLD with contraints
    
    pyhrf.verbose(1,"Fast EM with C extension started ...")
    np.random.seed(6537546)

    #######################################################################################################################
    #####################################################################################        INITIALIZATIONS
    #Initialize parameters
    tau1 = 0.0
    tau2 = 0.0
    S = 100
    Init_sigmaH = sigmaH
    Nb2Norm = 1
    NormFlag = False    
    if NitMax < 0:
        NitMax = 100
    gamma = 7.5#7.5
    #gamma_h = 1000
    gradientStep = 0.003
    MaxItGrad = 200
    Thresh = 1e-5
    Thresh_FreeEnergy = 1e-5
    estimateLabels=True #WARNING!! They should be estimated
    
    # Initialize sizes vectors
    D = int(np.ceil(Thrf/dt)) + 1 #D = int(numpy.ceil(Thrf/dt)) 
    M = len(Onsets)
    N = Y.shape[0]
    J = Y.shape[1]
    l = int(sqrt(J))
    condition_names = []

    # Neighbours
    maxNeighbours = max([len(nl) for nl in graph])
    neighboursIndexes = np.zeros((J, maxNeighbours), dtype=np.int32)
    neighboursIndexes -= 1
    for i in xrange(J):
        neighboursIndexes[i,:len(graph[i])] = graph[i]
    # Conditions
    X = OrderedDict([])
    for condition,Ons in Onsets.iteritems():
        X[condition] = vt.compute_mat_X_2(N, TR, D, dt, Ons)
        condition_names += [condition]
    XX = np.zeros((M,N,D),dtype=np.int32)
    nc = 0
    for condition,Ons in Onsets.iteritems():
        XX[nc,:,:] = X[condition]
        nc += 1
    # Covariance matrix
    order = 2
    D2 = vt.buildFiniteDiffMatrix(order,D)
    R = np.dot(D2,D2) / pow(dt,2*order)
    invR = np.linalg.inv(R)
    Det_invR = np.linalg.det(invR)
    
    Gamma = np.identity(N)
    Det_Gamma = np.linalg.det(Gamma)

    p_Wtilde = np.zeros((M,K),dtype=np.float64)
    p_Wtilde1 = np.zeros((M,K),dtype=np.float64)
    p_Wtilde[:,1] = 1

    Crit_H = 1
    Crit_Z = 1
    Crit_A = 1
    Crit_AH = 1
    AH = np.zeros((J,M,D),dtype=np.float64)
    AH1 = np.zeros((J,M,D),dtype=np.float64)
    Crit_FreeEnergy = 1
    
    cA = []
    cH = []
    cZ = []
    cAH = []
    FreeEnergy_Iter = []
    cTime = []
    cFE = []
    
    SUM_q_Z = [[] for m in xrange(M)]
    mu1 = [[] for m in xrange(M)]
    h_norm = []
    h_norm2 = []
    
    CONTRAST = np.zeros((J,len(contrasts)),dtype=np.float64)
    CONTRASTVAR = np.zeros((J,len(contrasts)),dtype=np.float64)
    Q_barnCond = np.zeros((M,M,D,D),dtype=np.float64)
    XGamma = np.zeros((M,D,N),dtype=np.float64)
    m1 = 0
    for k1 in X: # Loop over the M conditions
        m2 = 0
        for k2 in X:
            Q_barnCond[m1,m2,:,:] = np.dot(np.dot(X[k1].transpose(),Gamma),X[k2])
            m2 += 1
        XGamma[m1,:,:] = np.dot(X[k1].transpose(),Gamma)
        m1 += 1
    
    if MiniVEMFlag: 
        pyhrf.verbose(1,"MiniVEM to choose the best initialisation...")
        InitVar, InitMean, gamma_h = MiniVEM_CompMod(Thrf,TR,dt,beta,Y,K,gamma,gradientStep,MaxItGrad,D,M,N,J,S,maxNeighbours,neighboursIndexes,XX,X,R,Det_invR,Gamma,Det_Gamma,p_Wtilde,scale,Q_barnCond,XGamma,tau1,tau2,NbItMiniVem,sigmaH,estimateHRF)

    sigmaH = Init_sigmaH
    sigma_epsilone = np.ones(J)
    pyhrf.verbose(3,"Labels are initialized by setting active probabilities to ones ...")
    q_Z = np.zeros((M,K,J),dtype=np.float64)
    q_Z[:,1,:] = 1
    q_Z1 = np.zeros((M,K,J),dtype=np.float64)   
    Z_tilde = q_Z.copy()
    
    #TT,m_h = getCanoHRF(Thrf-dt,dt) #TODO: check
    TT,m_h = getCanoHRF(Thrf,dt) #TODO: check
    m_h = m_h[:D]
    m_H = np.array(m_h).astype(np.float64)
    m_H1 = np.array(m_h)
    sigmaH1 = sigmaH
    if estimateHRF:
        Sigma_H = np.ones((D,D),dtype=np.float64)
    else:
        Sigma_H = np.zeros((D,D),dtype=np.float64)
    
    Beta = beta * np.ones((M),dtype=np.float64)
    P = vt.PolyMat( N , 4 , TR)
    L = vt.polyFit(Y, TR, 4,P)
    PL = np.dot(P,L)
    y_tilde = Y - PL
    Ndrift = L.shape[0]

    sigma_M = np.ones((M,K),dtype=np.float64)
    sigma_M[:,0] = 0.5
    sigma_M[:,1] = 0.6
    mu_M = np.zeros((M,K),dtype=np.float64)
    for k in xrange(1,K):
        mu_M[:,k] = InitMean
    Sigma_A = np.zeros((M,M,J),np.float64)
    for j in xrange(0,J):
        Sigma_A[:,:,j] = 0.01*np.identity(M)    
    m_A = np.zeros((J,M),dtype=np.float64)
    m_A1 = np.zeros((J,M),dtype=np.float64)    
    for j in xrange(0,J):
        for m in xrange(0,M):
            for k in xrange(0,K):
                m_A[j,m] += np.random.normal(mu_M[m,k], np.sqrt(sigma_M[m,k]))*q_Z[m,k,j]
    m_A1 = m_A        
            
    t1 = time.time()
    
    #######################################################################################################################
    ####################################################################################    VBJDE num. iter. minimum

    ni = 0
    
    while ((ni < NitMin) or (((Crit_FreeEnergy > Thresh_FreeEnergy) or (Crit_AH > Thresh)) and (ni < NitMax))):
        
        pyhrf.verbose(1,"------------------------------ Iteration n° " + str(ni+1) + " ------------------------------")
        
        #####################
        # EXPECTATION
        #####################
        
        # A 
        pyhrf.verbose(3, "E A step ...")
        UtilsC.expectation_A(q_Z,mu_M,sigma_M,PL,sigma_epsilone,Gamma,Sigma_H,Y,y_tilde,m_A,m_H,Sigma_A,XX.astype(int32),J,D,M,N,K)
        val = reshape(m_A,(M*J))
        val[ np.where((val<=1e-50) & (val>0.0)) ] = 0.0
        val[ np.where((val>=-1e-50) & (val<0.0)) ] = 0.0
        
        # crit. A
        DIFF = reshape( m_A - m_A1,(M*J) )
        DIFF[ np.where( (DIFF<1e-50) & (DIFF>0.0) ) ] = 0.0 #### To avoid numerical problems
        DIFF[ np.where( (DIFF>-1e-50) & (DIFF<0.0) ) ] = 0.0 #### To avoid numerical problems
        Crit_A = (np.linalg.norm(DIFF) / np.linalg.norm( reshape(m_A1,(M*J)) ))**2
        cA += [Crit_A]
        m_A1[:,:] = m_A[:,:]
        
        # HRF h
        if estimateHRF:
            ################################
            #  HRF ESTIMATION
            ################################
            UtilsC.expectation_H(XGamma,Q_barnCond,sigma_epsilone,Gamma,R,Sigma_H,Y,y_tilde,m_A,m_H,Sigma_A,XX.astype(int32),J,D,M,N,scale,sigmaH)
            
            import cvxpy as cvx
            # data: Sigma_H, m_H
            m,n = Sigma_H.shape    
            Sigma_H_inv = np.linalg.inv(Sigma_H)
            zeros_H = np.zeros_like(m_H[:,np.newaxis])
            
            # Construct the problem. PRIMAL
            h = cvx.Variable(n)
            expression = cvx.quad_form(h - m_H[:,np.newaxis], Sigma_H_inv) 
            objective = cvx.Minimize(expression)
            constraints = [h[0] == 0, h[-1]==0, h >= zeros_H, cvx.square(cvx.norm(h,2))<=1]    
            prob = cvx.Problem(objective, constraints)
            result = prob.solve(verbose=1,solver=cvx.CVXOPT)            

            # Now we update the mean of h 
            m_H_old = m_H  
            Sigma_H_old = Sigma_H
            m_H = np.squeeze(np.array((h.value)))            
            Sigma_H = np.zeros_like(Sigma_H)    
            
            h_norm += [norm(m_H)]
            print 'h_norm = ', h_norm
            
            # Plotting HRF
            if PLOT and ni >= 0:
                import matplotlib.pyplot as plt
                plt.figure(M+1)
                plt.plot(m_H)
                plt.hold(True)
        else:
            if TrueHrfFlag:
                #TrueVal, head = read_volume(HrfFilename)
                TrueVal, head = read_volume(HrfFilename)[:,0,0,0]
                print TrueVal
                print TrueVal.shape
                m_H = TrueVal
        
        # crit. h
        Crit_H = (np.linalg.norm( m_H - m_H1 ) / np.linalg.norm( m_H1 ))**2
        cH += [Crit_H]
        m_H1[:] = m_H[:]

        # crit. AH
        for d in xrange(0,D):
            AH[:,:,d] = m_A[:,:]*m_H[d]
        DIFF = reshape( AH - AH1,(M*J*D) )
        DIFF[ np.where( (DIFF<1e-50) & (DIFF>0.0) ) ] = 0.0 #### To avoid numerical problems
        DIFF[ np.where( (DIFF>-1e-50) & (DIFF<0.0) ) ] = 0.0 #### To avoid numerical problems
        Crit_AH = (np.linalg.norm(DIFF) / np.linalg.norm( reshape(AH1,(M*J*D)) ))**2
        cAH += [Crit_AH]
        AH1[:,:,:] = AH[:,:,:]
        
        # Z labels
        if estimateLabels:
            pyhrf.verbose(3, "E Z step ...")
            if MFapprox:
                UtilsC.expectation_Z(Sigma_A,m_A,sigma_M,Beta,Z_tilde,mu_M,q_Z,neighboursIndexes.astype(int32),M,J,K,maxNeighbours)
            if not MFapprox:
                UtilsC.expectation_Z_ParsiMod_RVM_and_CompMod(Sigma_A,m_A,sigma_M,Beta,mu_M,q_Z,neighboursIndexes.astype(int32),M,J,K,maxNeighbours)
                #UtilsC.expectation_Z_ParsiMod_3(Sigma_A,m_A,sigma_M,Beta,p_Wtilde,mu_M,q_Z,neighboursIndexes.astype(int32),M,J,K,maxNeighbours)
        else:
            pyhrf.verbose(3, "Using True Z ...")
            TrueZ = read_volume(LabelsFilename)
            for m in xrange(M):
                q_Z[m,1,:] = reshape(TrueZ[0][:,:,:,m],J)
                q_Z[m,0,:] = 1 - q_Z[m,1,:]            
        
        # crit. Z 
        val = reshape(q_Z,(M*K*J))
        val[ np.where((val<=1e-50) & (val>0.0)) ] = 0.0
        
        DIFF = reshape( q_Z - q_Z1,(M*K*J) )
        DIFF[ np.where( (DIFF<1e-50) & (DIFF>0.0) ) ] = 0.0 #### To avoid numerical problems
        DIFF[ np.where( (DIFF>-1e-50) & (DIFF<0.0) ) ] = 0.0 #### To avoid numerical problems
        Crit_Z = ( np.linalg.norm(DIFF) / np.linalg.norm( reshape(q_Z1,(M*K*J)) ))**2
        cZ += [Crit_Z]
        q_Z1[:,:,:] = q_Z[:,:,:]
        
        #####################
        # MAXIMIZATION
        #####################
        
        # HRF: Sigma_h
        if estimateHRF:
            if estimateSigmaH:
                pyhrf.verbose(3,"M sigma_H step ...")
                if gamma_h > 0:
                    sigmaH = vt.maximization_sigmaH_prior(D,Sigma_H_old,R,m_H_old,gamma_h)
                else:
                    sigmaH = vt.maximization_sigmaH(D,Sigma_H,R,m_H)
                pyhrf.verbose(3,'sigmaH = ' + str(sigmaH))

        # (mu,sigma)
        pyhrf.verbose(3,"M (mu,sigma) step ...")
        mu_M, sigma_M = vt.maximization_mu_sigma(mu_M,sigma_M,q_Z,m_A,K,M,Sigma_A)
        for m in xrange(M):
            SUM_q_Z[m] += [sum(q_Z[m,1,:])]
            mu1[m] += [mu_M[m,1]]
        
        # Drift L
        UtilsC.maximization_L(Y,m_A,m_H,L,P,XX.astype(int32),J,D,M,Ndrift,N)
        PL = np.dot(P,L)
        y_tilde = Y - PL
        
        # Beta
        if estimateBeta:
            pyhrf.verbose(3,"estimating beta")
            for m in xrange(0,M):
                if MFapprox:
                    Beta[m] = UtilsC.maximization_beta(beta,q_Z[m,:,:].astype(float64),Z_tilde[m,:,:].astype(float64),J,K,neighboursIndexes.astype(int32),gamma,maxNeighbours,MaxItGrad,gradientStep)
                if not MFapprox:
                    #Beta[m] = UtilsC.maximization_beta(beta,q_Z[m,:,:].astype(float64),q_Z[m,:,:].astype(float64),J,K,neighboursIndexes.astype(int32),gamma,maxNeighbours,MaxItGrad,gradientStep)
                    Beta[m] = UtilsC.maximization_beta_CB(beta,q_Z[m,:,:].astype(float64),J,K,neighboursIndexes.astype(int32),gamma,maxNeighbours,MaxItGrad,gradientStep)
            pyhrf.verbose(3,"End estimating beta")
            pyhrf.verbose.printNdarray(3, Beta)
        
        # Sigma noise
        pyhrf.verbose(3,"M sigma noise step ...")
        UtilsC.maximization_sigma_noise(Gamma,PL,sigma_epsilone,Sigma_H,Y,m_A,m_H,Sigma_A,XX.astype(int32),J,D,M,N)
        
        #### Computing Free Energy ####
        if ni > 0:
            FreeEnergy1 = FreeEnergy
        FreeEnergy = vt.Compute_FreeEnergy(y_tilde,m_A,Sigma_A,mu_M,sigma_M,m_H,Sigma_H,R,Det_invR,sigmaH,p_Wtilde,tau1,tau2,q_Z,neighboursIndexes,maxNeighbours,Beta,sigma_epsilone,XX,Gamma,Det_Gamma,XGamma,J,D,M,N,K,S,"CompMod")
        if ni > 0:
            Crit_FreeEnergy = (FreeEnergy1 - FreeEnergy) / FreeEnergy1
        FreeEnergy_Iter += [FreeEnergy]
        cFE += [Crit_FreeEnergy]
        
        # Update index
        ni += 1
        
        t02 = time.time()
        cTime += [t02-t1]
    
    t2 = time.time()
    
    #######################################################################################################################
    ####################################################################################    PLOTS and SNR computation
    
    FreeEnergyArray = np.zeros((ni),dtype=np.float64)
    for i in xrange(ni):
        FreeEnergyArray[i] = FreeEnergy_Iter[i]

    SUM_q_Z_array = np.zeros((M,ni),dtype=np.float64)
    mu1_array = np.zeros((M,ni),dtype=np.float64)
    h_norm_array = np.zeros((ni),dtype=np.float64)
    for m in xrange(M):
        for i in xrange(ni):
            SUM_q_Z_array[m,i] = SUM_q_Z[m][i]
            mu1_array[m,i] = mu1[m][i]
            h_norm_array[i] = h_norm[i]

    if PLOT:
        import matplotlib.pyplot as plt
        import matplotlib
        font = {'size'   : 15}
        matplotlib.rc('font', **font)
        plt.savefig('./HRF_Iter_CompMod.png')
        plt.hold(False)
        plt.figure(2)
        plt.plot(cAH[1:-1],'lightblue')
        plt.hold(True)
        plt.plot(cFE[1:-1],'m')
        plt.hold(False)
        #plt.legend( ('CA','CH', 'CZ', 'CAH', 'CFE') )
        plt.legend( ('CAH', 'CFE') )
        plt.grid(True)
        plt.savefig('./Crit_CompMod.png')
        plt.figure(3)
        plt.plot(FreeEnergyArray)
        plt.grid(True)
        plt.savefig('./FreeEnergy_CompMod.png')

        plt.figure(4)
        for m in xrange(M):
            plt.plot(SUM_q_Z_array[m])
            plt.hold(True)
        plt.hold(False)
        #plt.legend( ('m=0','m=1', 'm=2', 'm=3') )
        #plt.legend( ('m=0','m=1') ) 
        plt.savefig('./Sum_q_Z_Iter_CompMod.png')
        
        plt.figure(5)
        for m in xrange(M):
            plt.plot(mu1_array[m])
            plt.hold(True)
        plt.hold(False)
        plt.savefig('./mu1_Iter_CompMod.png')
        
        plt.figure(6)
        plt.plot(h_norm_array)
        plt.savefig('./HRF_Norm_CompMod.png')
        
        Data_save = xndarray(h_norm_array, ['Iteration'])
        Data_save.save('./HRF_Norm_Comp.nii')        

    CompTime = t2 - t1
    cTimeMean = CompTime/ni

    sigma_M = sqrt(sqrt(sigma_M))
    pyhrf.verbose(1, "Nb iterations to reach criterion: %d" %ni)
    pyhrf.verbose(1, "Computational time = " + str(int( CompTime//60 ) ) + " min " + str(int(CompTime%60)) + " s")
    #print "Computational time = " + str(int( CompTime//60 ) ) + " min " + str(int(CompTime%60)) + " s"
    #print "sigma_H = " + str(sigmaH)
    if pyhrf.verbose.verbosity > 1:
        print 'mu_M:', mu_M
        print 'sigma_M:', sigma_M
        print "sigma_H = " + str(sigmaH)
        print "Beta = " + str(Beta)
        
    StimulusInducedSignal = vt.computeFit(m_H, m_A, X, J, N)
    SNR = 20 * np.log( np.linalg.norm(Y) / np.linalg.norm(Y - StimulusInducedSignal - PL) )
    SNR /= np.log(10.)
    print 'SNR comp =', SNR
    return ni,m_A,m_H, q_Z,sigma_epsilone,mu_M,sigma_M,Beta,L,PL,CONTRAST,CONTRASTVAR,cA[2:],cH[2:],cZ[2:],cAH[2:],cTime[2:],cTimeMean,Sigma_A,StimulusInducedSignal,FreeEnergyArray


    