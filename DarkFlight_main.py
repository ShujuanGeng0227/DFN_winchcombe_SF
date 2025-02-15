"""
Meteoroid Dark Flight Propagator

This dark flight model predicts the landing sight of a meteoroid by
propagating the position and velocity through the atmosphere using a
5th-order adaptive step size integrator (ODE45).

Created on Mon Oct 17 10:59:00 2016
@author: Trent Jansen-Sturgeon
"""

__author__ = "Trent Jansen-Sturgeon"
__copyright__ = "Copyright 2016, Desert Fireball Network"
__license__ = "MIT"
__version__ = "1.0"
__scriptName__ = "DarkFlightTrent.py"

import os
import sys
import argparse
import numpy as np
import math
import astropy.units as u
from astropy.time import Time
from numpy.linalg import norm
from scipy.stats import truncnorm
from astropy.table import Table, Column

# import matplotlib
# matplotlib.use('agg')
import matplotlib.pyplot as plt

from CSV2KML import Path, Points
from trajectory_utilities import ECEF2ECI, ENU2ECEF, ECI2ECEF, \
    ECI2ECEF_pos, ECEF2LLH, LLH2ECEF, EarthRadius, gravity_vector
# from darkfunctions_classic import viscosity, reynolds, \
#     knudsen, SoS, dragcoef
from atm_functions import dragcoeff, cd_hypersonic
from df_functions import WRF_history, WRF3D, WindDataExtraction, AtmosphericModel

from mpi4py import MPI
comm = MPI.COMM_WORLD
rank = comm.rank
size = comm.size

mu_e = 3.986005000e14 #4418e14 # Earth's standard gravitational parameter (m3/s2)


def random_perpendicular_unit_vector(a):
    # Step 1: Calculate the magnitude of vector a
    n_a = np.linalg.norm(a)

    # Step 2: Generate two random non-parallel vectors
    b = np.random.rand(3)

    pb = b.dot(a) * a / n_a ** 2
    b = np.array([[b[0]],[b[1]],[b[2]]])
    b = b-pb

    b /= np.linalg.norm(b)
    b = np.array(b)

    # Step 3: Calculate the cross product between a and b
    a = [a[0][0],a[1][0],a[2][0]]
    b = [b[0][0],b[1][0],b[2][0]]

    cross_product = np.cross(a, b)
    # Step 4: Normalize the cross product to get the perpendicular unit vector
    perpendicular_unit_vector = cross_product / np.linalg.norm(cross_product)

    return perpendicular_unit_vector

def EarthDynamics(t, X, WindData, t0, cl, return_abs_mag=False):
    '''
    The state rate dynamics are used in Runge-Kutta integration method to 
    calculate the next set of equinoctial element values.
    ''' 

    ''' State Rates '''
    # State parameter vector decomposed
    Pos_ECI = np.vstack((X[:3])); Vel_ECI = np.vstack((X[3:6]))
    M = X[6]; rho = X[7]; A = X[8]; c_ml = X[9] # c_massloss = sigma*cd_hyp
    t_jd = t0 + t / (24*60*60) # Absolute time [jd]
    ''' Primary Gravitational Acceleration '''
    a_grav = gravity_vector(Pos_ECI)
    ''' Atmospheric Drag Perturbation - Better Model Needed '''
    # Atmospheric velocity
    if type(WindData) == Table: #1D vertical profile
        [v_atm, rho_a, temp] = AtmosphericModel(WindData, Pos_ECI, t_jd)

    else: #3D wind profile
        Pos_ECEF = ECI2ECEF_pos(Pos_ECI, t_jd)
        Pos_LLH = ECEF2LLH(Pos_ECEF)

        [Wind_ENU, rho_a, temp] = WRF3D(WindData, Pos_LLH)

        Wind_ECEF = ENU2ECEF(Pos_LLH[1], Pos_LLH[0]).dot(Wind_ENU)
        v_atm = ECEF2ECI(Pos_ECEF, Wind_ECEF, t_jd)[1]


    # Velocity relative to the atmosphere
    v_rel = Vel_ECI - v_atm
    v = norm(v_rel)

    # New drag equations - function that fits the literature
    cd = dragcoeff(v, temp, rho_a, A)[0]

    # Total drag perturbation
    a_drag = -cd * A * rho_a * v * v_rel / (2 * M ** (1. / 3) * rho ** (2. / 3))
    v_rel3 = [v_rel[0][0],v_rel[1][0],v_rel[2][0]]
    # n_lift3 = random_perpendicular_unit_vector(v_rel) # a random unit vector normal to velocity

    # generate a lift unit vector to the discrepancy direction
    a_grav3 = [a_grav[0][0],a_grav[1][0],a_grav[2][0]]
    n_lift3 = np.cross(a_grav3,v_rel3) / norm(np.cross(a_grav3,v_rel3))
    n_lift = np.array(n_lift3).reshape(3,1)

    a_lift = cl * A * rho_a * v**2 * n_lift / (2 * M ** (1. / 3) * rho ** (2. / 3))

    ''' Total Perturbing Acceleration '''
    a_tot = a_grav + a_drag + a_lift

    # Mass-loss equation
    dm_dt = -c_ml * A * rho_a * v**3 * M**(2./3) / (2 * rho**(2./3))


    # See (Sansom, 2019) as reference
    if return_abs_mag: # sigma = c_ml / cd
        lum = -X[10] * (v**2 / 2 + cd / c_ml) * dm_dt * 1e7
        return -2.5 * np.log10(lum / 1.5e10)
    
    ''' State Rate Equation '''
    X_dot = np.zeros(X.shape)
    X_dot[:3] = Vel_ECI.flatten()
    X_dot[3:6] = a_tot.flatten()
    X_dot[6] = dm_dt

    return X_dot


def InitialiseCFG(TriData, mc, shapeFactor):

    # Position
    lat = np.deg2rad(Config.getfloat('met', 'lat0'))
    lon = np.deg2rad(Config.getfloat('met', 'lon0'))
    hei = Config.getfloat('met', 'z0')
    if mc:
        lat_err = np.deg2rad(Config.getfloat('met', 'dlat'))
        lon_err = np.deg2rad(Config.getfloat('met', 'dlon'))
        hei_err = Config.getfloat('met', 'dz')
        lat = np.random.normal(lat, lat_err, mc)
        lon = np.random.normal(lon, lon_err, mc)
        hei = np.random.normal(hei, hei_err, mc)
    Pos_LLH0 = np.vstack((lat, lon, hei))
    Pos_ECEF0 = LLH2ECEF(Pos_LLH0)

    # Velocity
    vel = Config.getfloat('met', 'vtot0')
    zen = np.deg2rad(Config.getfloat('met', 'zenangle'))
    azi = np.deg2rad(Config.getfloat('met', 'azimuth0'))
    rho = Config.getfloat('met', 'rdens0') # density of the meteoroid
    if mc:
        vel_err = Config.getfloat('met', 'dvtot')
        zen_err = np.deg2rad(Config.getfloat('met', 'dzenith'))
        azi_err = np.deg2rad(Config.getfloat('met', 'dazimuth0'))
        vel = np.random.normal(vel, vel_err, mc)
        zen = np.random.normal(zen, zen_err, mc)
        azi0 = np.random.normal(azi, azi_err, mc)

        ## set the variation of angle caused by v_lateral
        # the atmospheric density at the initial point 
        # (an estimated value based on a curve fitting 1976 us standard model)
        rho_aI = -140.2 * np.exp(-0.000187 * hei) + 141.4 * np.exp(-0.000186 * hei)
        # lateral velocity
        c_s = Config.getfloat('met', 'c_s')
        C_ltr = (c_s * rho_aI / rho)**0.5
        v_ltr = vel * C_ltr
        vel = (vel**2+v_ltr**2)**0.5
        # direction of lateral velocity, (normal to the fall line and parellal with the ground)
        a_break = np.random.choice([np.pi/2,-np.pi/2], size=mc)
        sinda = C_ltr*np.sin(a_break) / (np.sin(zen)**2 + (C_ltr*np.sin(a_break)) ** 2) ** 0.5
        d_azimuth = np.arcsin(sinda)
        azi = azi0 + d_azimuth
    Vel_ENU0 = vel * np.vstack((np.sin(zen)*np.sin(azi),np.sin(zen)*np.cos(azi),-np.cos(zen)))

    if mc:
        Vel_ECEF0 = np.hstack([ENU2ECEF(lon[i], lat[i]).dot(Vel_ENU0[:,i:i+1]) for i in range(mc)])
    else:
        Vel_ECEF0 = ENU2ECEF(lon, lat).dot(Vel_ENU0)

    # Time - no time in config file..
    # t0 = Time(Config.get('met', 'exposure_time'), format='isot', scale='utc').jd
    t0 = Time(Config.get('met', 'jd0'), format='jd', scale='utc').jd
    if mc:
        t0 = t0 * np.ones(mc)


    m_min = Config.getfloat('met', 'm_min')
    m_max = Config.getfloat('met', 'm_max')
    m_sigma = Config.getfloat('met', 'm_sigma')
    b_m = (m_max-m_min)/m_sigma
    
    if mc:
        M = truncnorm.rvs(loc=m_min, scale=m_sigma, a=0, b=b_m, size=mc)
        rho = np.ones(mc)*rho
        shape = np.random.normal(1.4, 0.3, size=mc)
    else:
        M = Config.getfloat('met', 'mass0')
        shape = shapeFactor


    ParameterDict = InitialiseParams(rho, shape)
    A = ParameterDict['shape']; c_ml = ParameterDict['c_ml']

    [Pos_ECI0, Vel_ECI0] = ECEF2ECI(Pos_ECEF0, Vel_ECEF0, t0)

    return {'time_jd':t0, 'pos_eci':Pos_ECI0, 'vel_eci':Vel_ECI0,
            'mass':M, 'rho':rho, 'shape':A, 'c_ml':c_ml, 'weight':np.ones(len(M))}

def InitialiseParams(rho, shape):
    if isinstance(rho, float): rho = np.array([rho])
    A = shape
    # Special cases:
    cd_hyp = cd_hypersonic(A) # Maybe should be cd(vel, temp, rho_a, rho, M, A)
    c_ml = 0.042e-6 * cd_hyp
    if len(rho) == 1: c_ml = float(c_ml)
    return {'shape':A, 'c_ml':c_ml}


###################################################################################
from scipy.integrate import ode
# from scipy.interpolate import interp1d
def Propagate(state0, args):
    '''
    Inputs: Initial ECI position (m), ECI velocity (m/s), and mass (kgs).
    Outputs: ECI position (m), ECI velocity (m/s) throughout the dark flight.
    '''
    # Calculate the meteor's initial state parameters
    Pos_ECI = np.vstack((state0[1:4])); t0 = float(state0[0])
    X = state0.flatten()[1:] # Add mass to the state 
    [WindData, h_ground, c_lift] = args

    # Initialise the time step
    R_sealevel = EarthRadius(ECEF2LLH(Pos_ECI)[0])
    r_end = R_sealevel + h_ground

    state, T_rel = [], []
    def solout(t, X):
        state.extend([X.copy()]); T_rel.extend([t])
        if norm(X[:3]) < r_end: # Reached ground
            return -1 # Ends the integration
        elif X[6] < 1e-3: # Lost all mass [<1g]
            print('Your meteoroid is dust!')
            print(norm(X[:3])-r_end)
            return -1 # Ends the integration
        elif X[6] < 0: # Lost all mass [<1g]
            print('Totally ablated')
            print(norm(X[:3])-r_end)
            return -1 # Ends the integration
        else:
            return 0 # Continues integration

    # Setup integrator
    dt0 = 0.000001; dt_max = 5
    solver = ode(EarthDynamics).set_integrator('dopri5', \
        first_step=dt0, max_step=dt_max, rtol=1e-4, atol=1e-6, nsteps = 1000)
    solver.set_solout(solout)
    solver.set_initial_value(X, 0).set_f_params(WindData, t0, c_lift)

    # Integrate with RK4 until impact
    t_max = np.inf; solver.integrate(t_max)

    # Assign the variables
    T = np.array(T_rel)/(24*60*60) + t0
    X_all = np.array(state).T
    
    # Make sure we end on the ground
    r_end = EarthRadius(ECEF2LLH(X_all[:3,-1:])[0]) + h_ground
    fraction = (norm(X_all[:3,-2:-1]) - r_end)/(norm(X_all[:3,-2:-1]) - norm(X_all[:3,-1:]))
    print(fraction)
    if len(X_all[0]) > 1:
        X_all[:,-1:] = X_all[:,-2:-1] + fraction * (X_all[:,-1:] - X_all[:,-2:-1])
        T[-1] = T[-2] + fraction * (T[-1] - T[-2])

    Pos_ECI = X_all[:3]; Vel_ECI = X_all[3:6]; M = X_all[6];     
    rho = X_all[7]; A = X_all[8]; c_ml = X_all[9]

    return {'time_jd':T, 'pos_eci':Pos_ECI, 'vel_eci':Vel_ECI,
            'mass':M, 'rho':rho, 'shape':A, 'c_ml':c_ml,'fraction':fraction}

def PropagateMultiple(State0, args):

    N_outputs = np.shape(State0)[1]
    Pos_ECI = np.zeros((3, N_outputs)); Vel_ECI = np.zeros((3, N_outputs))
    T = np.zeros(N_outputs); M = np.zeros(N_outputs); rho = np.zeros(N_outputs); 
    A = np.zeros(N_outputs); c_ml = np.zeros(N_outputs)
    Fract = np.zeros(N_outputs)
    # Repeat the darkflight for all the entries
    for i in range(N_outputs):

        # Print progress
        if rank == 0:
            sys.stdout.write('\rCalculating darkflight %d of %d...' % (i+1,N_outputs))
            sys.stdout.flush()
        
        # Propagate to the ground
        DarkDict = Propagate(State0[:,i:i+1], args)

        Pos_ECI[:,i:i+1] = DarkDict['pos_eci'][:,-1:]
        Vel_ECI[:,i:i+1] = DarkDict['vel_eci'][:,-1:]
        T[i] = DarkDict['time_jd'][-1]
        M[i] = DarkDict['mass'][-1]
        rho[i] = DarkDict['rho'][-1]
        A[i] = DarkDict['shape'][-1]
        c_ml[i] = DarkDict['c_ml'][-1]
        Fract[i] = DarkDict['fraction'][-1]
    return {'time_jd':T, 'pos_eci':Pos_ECI, 'vel_eci':Vel_ECI,
            'mass':M, 'rho':rho, 'shape':A, 'c_ml':c_ml, 'fraction':Fract}

###################################################################################
def WriteToFile(DATA, args):

    [ifile, WindFile, ofile2, kml, shape, mc] = args

    [Pos_ECI, Pos_ECEF, Pos_LLH, Vel_ECEF, Vel_mag, T, M0, M, rho, A, c_ml, W, fraction] = \
    [DATA.T[:3], DATA.T[3:6], DATA.T[6:9], DATA.T[9:12], DATA.T[12], DATA.T[13], DATA.T[14],
            DATA.T[15], DATA.T[16], DATA.T[17], DATA.T[18], DATA.T[19], DATA.T[20]]

    # Construct the DarkFile table
    datetime_col = Column(name='datetime', data=Time(T, format='jd', scale='utc').isot)
    JD_col = Column(name='JD', data=T*u.d)
    W_col = Column(name='weight', data=W)
    M0_col = Column(name='mass0', data=M0*u.kg)
    M_col = Column(name='mass', data=M*u.kg)
    F_col = Column(name='fraction', data=fraction)
    rho_col = Column(name='rho', data=rho*u.kg/u.m**3)
    shape_col = Column(name='shape', data=A)
    c_ml_col = Column(name='c_ml', data=c_ml*u.s**2/u.m**2)
    lat_col = Column(name='latitude', data=Pos_LLH[0]*u.rad.to(u.deg))
    lon_col = Column(name='longitude', data=Pos_LLH[1]*u.rad.to(u.deg))
    hei_col = Column(name='height', data=Pos_LLH[2]*u.m)
    X_geo_col = Column(name='X_geo', data=Pos_ECEF[0]*u.m)
    Y_geo_col = Column(name='Y_geo', data=Pos_ECEF[1]*u.m)
    Z_geo_col = Column(name='Z_geo', data=Pos_ECEF[2]*u.m)
    DX_DT_geo_col = Column(name='DX_DT_geo', data=Vel_ECEF[0]*u.m/u.s)
    DY_DT_geo_col = Column(name='DY_DT_geo', data=Vel_ECEF[1]*u.m/u.s)
    DZ_DT_geo_col = Column(name='DZ_DT_geo', data=Vel_ECEF[2]*u.m/u.s)
    D_DT_geo_col = Column(name='D_DT_geo', data=Vel_mag*u.m/u.s)
    
    DarkTable = Table([datetime_col, JD_col, W_col, M0_col, M_col, rho_col,
                       shape_col, c_ml_col, lat_col, lon_col, hei_col, 
                       X_geo_col, Y_geo_col, Z_geo_col, 
                       DX_DT_geo_col, DY_DT_geo_col, DZ_DT_geo_col, 
                       D_DT_geo_col,F_col], meta={'WindFile': WindFile, 'shape': shape})
    
    fileType = ifile.split('.')[-1]
    if Pos_LLH[2][0] > 10e3: # Single fall
        ofile_ext = '.ecsv'
        if fileType ==  'ecsv':
            ofile1 = '_darkflight_' + str(int(M[0]*1000)) + 'g'
            ofile3 = '_' + shape
        elif fileType == 'cfg':
            ofile1 = '_darkflight_cfg_' + str(int(M[0]*1000)) + 'g'
            ofile3 = '_' + shape

    elif len(M) < 100: # Fall-line or small no particles
        ofile_ext = '.ecsv'
        if fileType ==  'ecsv':
            ofile1 = '_darkflight_fall_line'
            ofile3 = '_' + shape
        elif fileType ==  'cfg':
            ofile1 = '_darkflight_cfg_fall_line'
            ofile3 = '_' + shape

    else: # MC or particles
        ofile_ext = '.fits'; ofile3 = ''
        if fileType == 'ecsv' and mc:
            ofile1 = 'darkflight_'+str(mc)+'mc'
        elif fileType == 'fits':
            ofile1 = '_darkflight'
        elif fileType == 'cfg':
            ofile1 = '_darkflight_cfg_'+str(mc)+'mc'

    # Name the DarkFile
    import datetime; date_str = datetime.datetime.now().strftime('%Y%m%d')
    DarkDir = os.path.join(os.path.dirname(ifile), 'darkflight_'+date_str)
    if not os.path.isdir(DarkDir): # Create the directory if it doesn't exist
        os.mkdir(DarkDir)
    DarkFile = os.path.join(DarkDir,os.path.basename(ifile).split('.')[0] 
            + ofile1 + ofile2 + ofile3 + '_run0'+ ofile_ext); j = 1
    while os.path.exists(DarkFile): # Make sure the file name is unique
        DarkFile = '_'.join(DarkFile.split('.')[0].split('_')[:-1])+'_run'+str(j)+ofile_ext; j += 1

    # Write the DarkFile
    if ofile_ext == '.ecsv':
        DarkTable.write(DarkFile, format='ascii.ecsv', delimiter=',')
    elif ofile_ext == '.fits':
        DarkTable.write(DarkFile, format='fits')
    print('\n\nOutput has been written to: ' + DarkFile + '\n')

    # Make a KML of the trajectory
    if kml:
        if Pos_LLH[2][0] > 10e3: # Single fall
            Path(DarkFile)
        elif fileType != 'fits' and not mc: # Fall-line
            Points(DarkFile, np.round(M,3), colour='ff1400ff') # red points
        else:
            Points(DarkFile)
    print('')

    ####################################################
    # Plot the wind histories
    ####################################################
    if Pos_LLH[2][0] > 10e3: # Single fall

        WRF_hist = np.hstack((WRF_history))
        height_hist = WRF_hist[0]/1000

        plt.figure(figsize=(16,9))

        plt.subplot(1,3,(1,2))
        plt.plot(WRF_hist[1], height_hist, lw=1.5, label='East')
        plt.plot(WRF_hist[2], height_hist, lw=1.5, label='North')
        plt.plot(WRF_hist[3]*100, height_hist, lw=1.5, label='Up [x100]')
        plt.plot(norm(WRF_hist[1:4], axis=0), height_hist, 'k', lw=2, label='Total')
        plt.plot(-norm(WRF_hist[1:4], axis=0), height_hist, 'k', lw=2)
        plt.xlabel('Wind [m/s]'); plt.ylabel('Height [km]')
        plt.grid(True); plt.legend(loc=0)

        ax1 = plt.subplot(1,3,3); ax2 = ax1.twiny()
        line1, = ax1.plot(WRF_hist[4], height_hist, 'r', lw=2)
        line2, = ax2.plot(WRF_hist[5], height_hist, 'b', lw=2)
        ax1.set_xlabel('Atm Density [kg/m3]'); ax2.set_xlabel('Atm Temperature [K]'); 
        ax1.set_ylabel('Height [km]'); plt.grid(True)
        plt.legend((line1, line2),('Density [kg/m3]', 'Temperature [K]'))

        plt.savefig(os.path.join(DarkDir,'Atmosphere'+ofile1+ofile2+ofile3+'_run'+str(j-1)+'.png'), format='png')

    ####################################################
    # Plot the drag parameters
    ####################################################
    particles = np.shape(Pos_ECEF)[1]
    mach = np.zeros(particles); re = np.zeros(particles)
    kn = np.zeros(particles); cd = np.zeros(particles)
    for i in range(particles):

        # Atmospheric velocity
        if type(WindData) == Table: #1D vertical profile
            [v_wind, rho_a, temp] = AtmosphericModel(WindData, Pos_ECI[:,i:i+1], T[i])
        else:
            [v_wind, rho_a, temp] = WRF3D(WindData, Pos_LLH[:,i:i+1])

        # Velocity relative to the atmosphere
        v_rel = Vel_ECEF[:,i:i+1] - v_wind
        v = norm(v_rel, axis=0)


        [cd[i], re[i], kn[i], mach[i]] = dragcoeff(v, temp, rho_a, A[i])
    
    # cd /= cd

    if Pos_LLH[2][0] > 10e3: # Single fall
        ls = '-'
    else:
        ls = '.'

    T_rel = (T - np.min(T)) * 24*60*60
    plt.figure(figsize=(16,9))

    plt.subplot(2,3,1)
    plt.plot(re, cd, ls); plt.grid(True)
    plt.xlabel('Reynolds #'); plt.ylabel('Drag Coefficient')
    plt.xscale('log')

    plt.subplot(2,3,4)
    plt.plot(T_rel, re, ls); plt.grid(True)
    plt.xlabel('Relative Time [s]'); plt.ylabel('Reynolds #')
    plt.yscale('log')

    plt.subplot(2,3,6)
    plt.plot(T_rel, kn*1e6, ls); plt.grid(True)
    plt.xlabel('Relative Time [s]'); plt.ylabel('Knudson # [x1e6]')

    plt.subplot(2,3,2)
    plt.plot(mach, cd, ls); plt.grid(True)
    plt.xlabel('Mach #'); plt.ylabel('Drag Coefficient')

    plt.subplot(2,3,5)
    plt.plot(T_rel, mach, ls); plt.grid(True)
    plt.xlabel('Relative Time [s]'); plt.ylabel('Mach #')

    plt.subplot(2,3,3)
    plt.plot(T_rel, cd, ls); plt.grid(True)
    plt.xlabel('Relative Time [s]'); plt.ylabel('Drag Coefficient')

    plt.savefig(os.path.join(DarkDir,'cd_scatter'+ofile1+ofile2+ofile3+'_run'+str(j-1)+'.png'), format='png')

###################################################################################
if __name__ == '__main__':
    '''
    Darkflight code to determine the impact site of meteorite.
    Inputs: TriFile  - can be single (.ecsv) or multiple (particles) terminal point
            WindFile - the vertical wind profile taken near the end of event (default = None)
            PF       - toggle to identify the TriFile type (default = False)
            M        - mass (kg) for determining the darkflight (default = 'fall_line')
            KML      - whether you want a darkflight and/or impact KML file produced (default = True)
            h_ground - the Earth impact height above sea-level in meters (default = 0m)
    Outputs: Darkflight file (.ecsv) and possibly google earth file (.kml)
    '''
    # Gather some user defined information
    if rank == 0:
        parser = argparse.ArgumentParser(description='Darkflight meteoroids')
        parser.add_argument("-e", "--eventFile", type=str, required=True,
                help="Event file for propagation [.CFG]")
        parser.add_argument("-w", "--windFile", type=str,
                help="Wind file for the corresponding event [.CSV]")
        parser.add_argument("-s", "--shape", type=float, default=1.21,
                help="Specify the meteorite shape factor for the darkflight")
        parser.add_argument("-g", "--h_ground", type=float, default=0.,
                help="Height of the ground around landing site (m)")
        parser.add_argument("-l", "--lift_coefficient", type=float, default=0.,
                help="lift coefficient, generally 0.001-0.01, set this value when lift is considered")
        parser.add_argument("-k", "--kml", action="store_false", default=True,
                help="use this option if you don't want to generate KMLs")
        parser.add_argument("-mc", "--MonteCarlo", type=int, default=0,
                help="Number of Monte Carlo simulations for the darkflight")
        
        args = parser.parse_args()

        ifile = args.eventFile
        WindFile = args.windFile
        shape = args.shape
        h_ground = args.h_ground
        kml = args.kml
        mc = args.MonteCarlo
        c_l = args.lift_coefficient

        fileType = ifile.split('.')[-1]

        # Read in the triangulated data

        if os.path.isfile(ifile) and fileType == 'cfg':
            import configparser; Config = configparser.RawConfigParser()
            TriData = Config.read(ifile)
            DarkDict0 = InitialiseCFG(TriData, mc, shape)

        else:
            print('Your file does not exist :(')
            exit()

        # Read in the wind data
        if WindFile and os.path.exists(WindFile):
            if WindFile.endswith('.csv'):
                WindData = Table.read(WindFile, format='ascii.csv', guess=False, delimiter=',')
                WindFile = os.path.basename(WindFile)
                ofile2 = '_'+WindFile.split('.')[0].split('_')[-1]+'Wind' 
            else:
                WindData = WindDataExtraction(WindFile, DarkDict0['time_jd'])
                # WindData = netcdf.netcdf_file(WindFile, 'r', mmap=False)
                ofile2 = '_'+WindFile.split('_')[-1].replace(':','')[:4]+'Wind'
        else:
            WindData = Table(); ofile2 = '_NoWind'; WindFile = 'None'
            print('No wind file exists. Calculating windless darkflight.')

        Pos_ECI0 = DarkDict0['pos_eci']; Vel_ECI0 = DarkDict0['vel_eci']
        t0 = DarkDict0['time_jd']; M = DarkDict0['mass']; rho = DarkDict0['rho']
        A = DarkDict0['shape']; c_ml = DarkDict0['c_ml']; W = DarkDict0['weight']

        print('')
        # Collect all the particle parameters
        N = len(M); n = np.array([N//size]*size); n[:(N%size)] += 1
        # DATA0 = np.hstack((Pos_ECI0.T, Vel_ECI0.T, np.vstack((t0)), np.vstack((M)), np.vstack((S)))) #[N,10]
        DATA0 = np.zeros((N,12)); states0 = np.shape(DATA0)[1]
        DATA0[:,0] = t0; DATA0[:,1:4] = Pos_ECI0.T; DATA0[:,4:7] = Vel_ECI0.T
        DATA0[:,7] = M; DATA0[:,8] = rho; DATA0[:,9] = A
        DATA0[:,10] = c_ml; DATA0[:,11] = W

        data0 = np.zeros((n[rank], states0)) #[n,11]
        for i in range(1, size):
            comm.send([WindData, n, states0, h_ground], dest=i)
    else:
        [WindData, n, states0, h_ground] = comm.recv(source=0)
        DATA0 = None; data0 = np.zeros((n[rank], states0)) #[n,11]

    if not n[rank]: # if there are no particles on core
        print('Terminating additional core.'); exit()

    # Scatter the DATA0 amongst the ranks
    sendcounts = states0*n; displacements = np.hstack((0,np.cumsum(sendcounts)[:-1]))
    comm.Scatterv([DATA0, sendcounts, displacements, MPI.DOUBLE], data0, root=0)

    ############################################################################
    # Darkflight calcs for all ranks

    args = [WindData, h_ground, c_l]; M0 = data0.T[7]

    # [t0, Pos_ECI0, Vel_ECI0] = [data0.T[0], data0.T[1:4], data0.T[4:7]]

    # [t0, Pos_ECEF0, Vel_ECEF0] = [data0.T[0], data0.T[1:4], data0.T[4:7]]
    # [Pos_ECI0, Vel_ECI0] = ECEF2ECI(Pos_ECEF0, Vel_ECEF0, t0)

    State = data0.T[:11]; W = data0.T[11]
    # State[1:4] = Pos_ECI0; State[4:7] = Vel_ECI0
    if sum(n) == 1:
        print('Calculating single darkflight path...')
        DarkDict = Propagate(State, args)
        n = np.array([len(DarkDict['time_jd'])]+[0]*(size-1))
    else:
        DarkDict = PropagateMultiple(State, args)

    Pos_ECI = DarkDict['pos_eci']; Vel_ECI = DarkDict['vel_eci'];
    T = DarkDict['time_jd']; rho = DarkDict['rho']
    M = DarkDict['mass']; A = DarkDict['shape']; c_ml = DarkDict['c_ml']
    Fract = DarkDict['fraction']
    
    # Convert ECI to ECEF and LLH coords
    [Pos_ECEF, Vel_ECEF] = ECI2ECEF(Pos_ECI, Vel_ECI, T)
    Pos_LLH = ECEF2LLH(Pos_ECEF); Vel_mag = norm(Vel_ECEF, axis=0)

    ############################################################################

    # Gather all the data from the ranks to master
    # data = [Pos_ECEF, Pos_LLH, Vel_ECEF, Vel_mag, T, M0, M, rho, A, c_ml, W]
    data = np.zeros((n[rank],21)); data[:,:3] = Pos_ECI.T; data[:,3:6] = Pos_ECEF.T; data[:,6:9] = Pos_LLH.T
    data[:,9:12] = Vel_ECEF.T; data[:,12] = Vel_mag; data[:,13] = T; data[:,14] = M0
    data[:,15] = M; data[:,16] = rho; data[:,17] = A; data[:,18] = c_ml; data[:,19] = W;
    data[:,20] = Fract
    
    # [data[:3], data[3:6], data[6:9], data[9], data[10], data[11],
    #         data[12], data[13], data[14], data[15], data[16]] = \
    # [Pos_ECEF.T, Pos_LLH.T, Vel_ECEF.T, Vel_mag, T, M0, M, rho, A, c_ml, W]

    states = np.shape(data)[1]; DATA = np.zeros((np.sum(n), states))
    sendcounts = states*n; displacements = np.append(0,np.cumsum(sendcounts)[:-1])
    comm.Gatherv(data, [DATA, sendcounts, displacements, MPI.DOUBLE], root=0)

    if rank == 0: 
        args = [ifile, WindFile, ofile2, kml, shape, mc]
        WriteToFile(DATA, args)
