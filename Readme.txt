This is an adapted version of DFN_DarkFlight model, the winchcombe branch:
https://github.com/desertfireballnetwork/DFN_darkflight/tree/winchcombe

Adjustments were made by GENG Shujuan and Gareth S. COLLINS.

To run the program, you can do:
python DarkFlight_main.py -e data/winchcombe_35.cfg -w data/profile_DN210228_02_UK_start_02-28_1200.csv -g 300 -mc 100

Notes:
1. C_s: lateral coefficient
Change the c_s value in .cfg files if you want to explore the influence of the lateral coefficient, 
the default value is 0

2. C_l: lift
Use "-l" option if you want to explore the influence of lift, for example: python DarkFlight_main.py -e data/winchcombe_35.cfg -w data/profile_DN210228_02_UK_start_02-28_1200.csv -g 300 -l 0.0075 -mc 100
the default value is 0. The typical range of value of c_lift is 0.001-0.01

3. Initial height
Change the event file if you want to explore the influence of the initial height
winchcombe_end.cfg: initial height is ~27.5 km, the original .cfg used in https://onlinelibrary.wiley.com/doi/10.1111/maps.13977
winchcombe_35.cfg: initial height is ~35 km; 
winchcombe_40.cfg: initial height is ~40 km; 
winchcombe_45.cfg: initial height is ~45 km; 
the initial position and velocity in these three new .cfg files were obtained by interpolating the corresponding values of observed points. 
