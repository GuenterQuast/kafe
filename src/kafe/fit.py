'''
.. module:: fit
    :platform: Unix
    :synopsis: This submodule defines a `Fit` object which performs the actual
        fitting given a `Dataset` and a fit function.

.. moduleauthor:: Daniel Savoiu <danielsavoiu@gmail.com>
.. moduleauthor:: Guenter Quast <G.Quast@kit.edu>

'''

# ----------------------------------------------------------------
# Changes:
#  06-Aug-14  G.Q.  default parameter errors set to 10% (not 0.1%)
#  07-Aug-14  G.Q.  covariance matrix returned by Minuit contains zero-values
#                   for lines/colums corresponding to fixed parameters; now
#                   calling a special version, MinuitCov_to_cor in
#                   print_fit_results
#             G.Q.  adjusted output formats in print_fit_xxx: `g` format
#                    gives enough digits, '.02e' is sufficient for errors
#             G.Q. output of print_fit_xxx a little more compact;
#                   took account of possibly fixed parameters
#             G.Q. adjusted tolerance on cov mat. changes an # of iterations
# 10-Aug-14   G.Q. initial fits without running HESSE algorithm in Minuit
#                   introduced steering flag "final_fit"
# 11-Aug-14   G.Q. took into accout fixed parameters in calculation of ndf
#                  added mechanism to constrain parameters within error:
#                     - provided function constrain_parameters()
#                     - add corresponding chi2-Term in chi2
# -------------------------------------------------------------------------

from minuit import Minuit
from function_tools import FitFunction, outer_product
from copy import copy

import numpy as np
from numeric_tools import cov_to_cor, extract_statistical_errors, MinuitCov_to_cor

from config import FORMAT_ERROR_SIGNIFICANT_PLACES, F_SIGNIFICANCE_LEVEL
from math import floor, log

import os
from stream import StreamDup

# import main logger for kafe
import logging
logger = logging.getLogger('kafe')


# The default FCN
def chi2(xdata, ydata, cov_mat,
         fit_function, parameter_values,
         constrained_parameters=None):
    r'''
    The :math:`\chi^2` implementation. Calculates :math:`\chi^2` according
    to the formula:

    .. math::

        \chi^2 = \lambda^T C^{-1} \lambda


    Here, :math:`\lambda` is the residual vector :math:`\lambda = \vec{y} -
    \vec{f}(\vec{x})` and :math:`C` is the covariance matrix.

    If a constraint :math:`c_i\pm\sigma_i` is applied to  a parameter i,
    a `penalty term` is added for each constrained parameter according to:

    .. math::

        \chi^2 += \left( \frac{v_i - c_i}{\sigma_i} \right)^2


    **xdata** : iterable
        The *x* measurement data

    **ydata** : iterable
        The *y* measurement data

    **cov_mat** : `numpy.matrix`
        The total covariance matrix

    **fit_function** : function
        The fit function :math:`f(x)`

    **constrained_parameters** : None or list of two iterables
    with a length equal to the number of parameters. An uncertainty of
    0 means that a parameter remains unconstrained.

    **parameter_values** : list/tuple
        The values of the parameters at which :math:`f(x)` should be evaluated.
    '''

    # since the parameter_values are constants, the
    # fit function is a function of only one
    # variable: `x'. To apply it elementwise using
    # Python's `map' method, make a temporary
    # function where `x' is the only variable:
    def tmp_fit_function(x):
        return fit_function(x, *parameter_values)

    # calculate f(x) for all x in xdata
    fdata = np.asarray(map(tmp_fit_function, xdata))
    # calculate residual vector
    residual = ydata - fdata

    chi2val = (residual.T.dot(cov_mat.I).dot(residual))[0, 0]  # return the chi^2

    # apply constraints, if any
    if(constrained_parameters is not None):
        for i, err in enumerate(constrained_parameters[1]):
            if(err): # there is a constraint, add to chi2
                dchi2=(parameter_values[i]-constrained_parameters[0][i])/err
                dchi2*=dchi2
                chi2val+=dchi2

    return chi2val


def round_to_significance(value, error, significance=FORMAT_ERROR_SIGNIFICANT_PLACES):
    '''
    Rounds the error to the established number of significant digits, then
    rounds the value to the same order of magnitude as the error.

    **value** : float
        value to round to significance

    **error** : float
        uncertainty of the value

    *significance* : int (optional)
        number of significant digits of the error to consider

    '''
    # round error to FORMAT_ERROR_SIGNIFICANT_PLACES significant digits
    if error:
        significant_digits = int(-floor(log(error)/log(10))) + significance - 1
        error = round(error, significant_digits)
        value = round(value, significant_digits)

    return (value, error)


class Fit(object):
    '''
    Object representing a fit. This object references the fitted `Dataset`,
    the fit function and the resulting fit parameters.

    Necessary arguments are a `Dataset` object and a fit function (which should
    be fitted to the `Dataset`). Optionally, an external function `FCN` (the
    minimum of which should be located to find the best fit) can be specified.
    If not given, the `FCN` function defaults to :math:`\chi^2`.

    **dataset** : `Dataset`
        A `Dataset` object containing all information about the data

    **fit_function** : function
        A user-defined Python function to be fitted to the data. This
        function's first argument must be the independent variable `x`. All
        other arguments *must* be named and have default values given. These
        defaults are used as a starting point for the actual minimization. For
        example, a simple linear function would be defined like:

        >>> def linear_2par(x, slope=1., y_intercept=0.):
        ...     return slope * x + y_intercept

        Be aware that choosing sensible initial values for the parameters is
        often crucial for a succesful fit, particularly for functions of many
        parameters.

    *external_fcn* : function (optional)
        An external `FCN` (function to minimize). This function must have the
        following call signature:

        >>> FCN(xdata, ydata, cov_mat, fit_function, parameter_values)

        It should return a float. If not specified, the default :math:`\chi^2`
        `FCN` is used. This should be sufficient for most fits.

    *fit_label* : :math:`LaTeX`-formatted string (optional)
        A name/label/short description of the fit function. This appears in the
        legend describing the fitter curve. If omitted, this defaults to the
        fit function's :math:`LaTeX` expression.
    '''

    def __init__(self, dataset, fit_function, external_fcn=chi2,
                 fit_label=None):
        '''
        Construct an instance of a ``Fit``
        '''

        # Initialize instance variables
        ################################

        self.dataset = dataset  #: this Fit instance's child `Dataset`

        if isinstance(fit_function, FitFunction):
            #: the fit function used for this `Fit`
            self.fit_function = fit_function
        else:
            # if not done alreasy, apply the FitFunction
            # decorator to fit_function ("silent cast")
            self.fit_function = FitFunction(fit_function)

            # and report
            logger.info("Custom fit function not decorated with "
                        "FitFunction decorator; doing a silent cast.")

        #: the (external) function to be minimized for this `Fit`
        self.external_fcn = external_fcn

        #: the total number of parameters
        self.number_of_parameters = self.fit_function.number_of_parameters

        self.set_parameters(self.fit_function.parameter_defaults,
                            None, no_warning=True)

        #: the names of the parameters
        self.parameter_names = self.fit_function.parameter_names
        #: :math:`LaTeX` parameter names
        self.latex_parameter_names = \
            self.fit_function.latex_parameter_names

        # store a dictionary to lookup whether a parameter is fixed
        self._fixed_parameters = np.zeros(self.number_of_parameters,
                                         dtype=bool)
        self.number_of_fixed_parameters = 0

        # list to store values and errors of constrained parameters
        self._constrained_parameters = np.zeros(self.number_of_parameters,
                                         dtype=bool)
        self.constrained_parameters=[
            np.zeros(self.number_of_parameters,dtype=np.float32),
            np.zeros(self.number_of_parameters,dtype=np.float32)]
        self.number_of_constrained_parameters = 0

        # store the full function definition
        self.function_equation_full = \
            self.fit_function.get_function_equation('latex', 'full')

        # store a short version of the function's equation
        self.function_equation = \
            self.fit_function.get_function_equation('latex', 'short')

        self.fit_label = fit_label

        # check if the dataset has any y errors at all
        if self.dataset.has_errors('y'):
            # set the y cov_mat as starting cov_mat for the fit
            # and report if singular matrix
            self.current_cov_mat = self.dataset.get_cov_mat(
                'y',
                fallback_on_singular='report'
            )
            '''the current covariance matrix used for the `Fit`'''
        else:
            # set the identity matrix as starting cov_mat for the fit
            self.current_cov_mat = np.asmatrix(np.eye(self.dataset.get_size()))
            logger.info("No `y`-errors provided for dataset. Assuming all "
                        "data points have the `y`-error 1.0")

        #: this `Fit`'s minimizer (`Minuit`)
        self.minimizer = Minuit(self.number_of_parameters,
                                self.call_external_fcn, self.parameter_names,
                                self.current_parameter_values, None)

        # set Minuit's initial parameters and parameter errors
        #            may be overwritten via ``set_parameters``
        self.minimizer.set_parameter_values(self.current_parameter_values)
        self.minimizer.set_parameter_errors()  # default 10%, 0.1 if value==0.

        # store measurement data locally in Fit object
        #: the `x` coordinates of the data points used for this `Fit`
        self.xdata = self.dataset.get_data('x')
        #: the `y` coordinates of the data points used for this `Fit`
        self.ydata = self.dataset.get_data('y')

        # Define a stream for storing the output
        if self.dataset.basename is not None:
            _basename = self.dataset.basename
        else:
            _basename = 'untitled'
        _basenamelog = _basename+'.log'

        # check for old logs
        if os.path.exists(_basenamelog):
            logger.warning('Old log files found for fit `%s`. kafe will not '
                           'delete these files, but it is recommended to do '
                           'so, in order to reduce clutter.'
                           % (_basename,))

            # find first incremental name for which no file exists
            _id = 1
            while os.path.exists(_basename+'.'+str(_id)+'.log'):
                _id += 1

            # move existing log to that location
            os.rename(_basenamelog, _basename+'.'+str(_id)+'.log')

        self.out_stream = StreamDup(['fit.log', _basenamelog])


    def call_external_fcn(self, *parameter_values):
        '''
        Wrapper for the external `FCN`. Since the actual fit process depends on
        finding the right parameter values and keeping everything else constant
        we can use the `Dataset` object to pass known, fixed information to the
        external `FCN`, varying only the parameter values.

        **parameter_values** : sequence of values
            the parameter values at which `FCN` is to be evaluated

        '''

        return self.external_fcn(self.xdata, self.ydata, self.current_cov_mat,
                                 self.fit_function, parameter_values,
                                 self.constrained_parameters)

    def get_function_error(self, x):
        r'''
        This method uses the parameter error matrix of the fit to calculate
        a symmetric (parabolic) error on the function value itself. Note that
        this method takes the entire parameter error matrix into account, so
        that it also accounts for correlations.

        The method is useful if, e.g., you want to draw a confidence band
        around the function in your plot routine.

        **x** : `float` or sequence of `float`
            the values at which the function error is to be estimated

        returns : `float` or sequence of `float`
            the estimated error at the given point(s)
        '''

        try:
            iter(x)
        except:
            x = np.array([x])


        errors = np.zeros_like(x)
        # go through each data point and calculate the function error
        for i, fval in enumerate(x):
            # calculate the outer product of the gradient of f with itself
            # (with respect to the parameters)
            # use 1/100th of the smallest parameter error as spacing for df/dp
            derivative_spacing = 0.01 * np.sqrt(
                min(np.diag(self.get_error_matrix()))
            )
            par_deriv_outer_prod = outer_product(
                self.fit_function.derive_by_parameters(
                    fval,
                    derivative_spacing,
                    self.current_parameter_values
                )
            )

            tmp_sum = np.sum(
                par_deriv_outer_prod * np.asarray(
                    self.get_error_matrix()
                )
            )
            errors[i] = np.sqrt(tmp_sum)

        return errors

    def get_current_fit_function(self):
        '''
        This method returns a function object corresponding to the fit function
        for the current parameter values. The returned function is a function
        of a single variable.

        returns : function
            A function of a single variable corresponding to the fit function
            at the current parameter values.
        '''

        def current_fit_function(x):
            return self.fit_function(x, *self.current_parameter_values)

        return current_fit_function

    def get_error_matrix(self):
        '''
        This method returns the covariance matrix of the fit parameters which
        is obtained by querying the minimizer object for this `Fit`

        returns : `numpy.matrix`
            The covariance matrix of the parameters.
        '''
        return self.minimizer.get_error_matrix()

    def get_parameter_errors(self, rounding=False):
        '''
        Get the current parameter uncertainties from the minimizer.

        *rounding* : boolean (optional)
            Whether or not to round the returned values to significance.

        returns : tuple
            A tuple of the parameter uncertainties
        '''
        output = []
        for name, value, error in self.minimizer.get_parameter_info():

            if rounding:
                value, error = round_to_significance(value, error)
            output.append(error)

        return tuple(output)

    def get_parameter_values(self, rounding=False):
        '''
        Get the current parameter values from the minimizer.

        *rounding* : boolean (optional)
            Whether or not to round the returned values to significance.

        returns : tuple
            A tuple of the parameter values
        '''

        output = []
        for name, value, error in self.minimizer.get_parameter_info():

            if rounding:
                value, error = round_to_significance(value, error)
            output.append(value)

        return tuple(output)

    def set_parameters(self, *args, **kwargs):
        '''
        Sets the parameter values (and optionally errors) for this fit.
        This is usually called just before the fit is done, to establish
        the initial parameters. If a parameter error is omitted, it is
        set to 1/1000th of the parameter values themselves. If the default
        value of the parameter is 0, it is set, by exception, to 0.001.

        This method accepts up to two positional arguments and several
        keyword arguments.

        *args[0]* : tuple/list of floats (optional)
            The first positional argument is expected to be
            a tuple/list containing the parameter values.

        *args[1]* : tuple/list of floats (optional)
            The second positional argument is expected to be a
            tuple/list of parameter errors, which can also be set as an
            approximate estimate of the problem's uncertainty.

        *no_warning* : boolean (optional)
            Whether to issue warnings (``False``) or not (``True``) when
            communicating with the minimizer fails. Defaults to ``False``.

        Valid keyword argument names are parameter names. The keyword arguments
        themselves may be floats (parameter values) or 2-tuples containing the
        parameter values and the parameter error in that order:

        *<parameter_name>* : float or 2-tuple of floats (optional)
            Set the parameter with the name <'parameter_name'> to the value
            given. If a 2-tuple is given, the first element is understood
            to be the value and the second to be the parameter error.
        '''

        # Process arguments

        # get global keyword argument
        no_warning = kwargs.pop("no_warning", False)

        if args:  # if positional arguents provided
            if len(args) == 1:
                par_values, par_errors = args[0], None
            elif len(args) == 2:
                par_values, par_errors = args[0], args[1]
            else:
                raise Exception("Error setting parameters. The argument "
                                "pattern for method `set_parameters` could "
                                "not be parsed.")

            if len(par_values) == self.number_of_parameters:
                #: the current values of the parameters
                self.current_parameter_values = list(par_values)
            else:
                raise Exception("Cannot set parameters. Number of given "
                                "parameters (%d) doesn't match the Fit's "
                                "parameter number (%d)."
                                % (len(par_values), self.number_of_parameters))

            if par_errors is not None:
                if len(par_values) == self.number_of_parameters:
                    #: the current uncertainties of the parameters
                    self.current_parameter_errors = list(par_errors)
                else:
                    raise Exception("Cannot set parameter errors. Number of "
                                    "given parameter errors (%d) doesn't "
                                    "match the Fit's parameter number (%d)."
                                    % (len(par_errors),
                                       self.number_of_parameters))
            else:
                if not no_warning:
                    logger.warn("Parameter starting errors not given. Setting "
                                "to 1/10th of the parameter values.")
                #: the current uncertainties of the parameters
                self.current_parameter_errors = [
                    val/10. if val else 0.1  # handle the case val = 0
                    for val in self.current_parameter_values
                ]
        else:  # if no positional arguments, rely on keywords

            for param_name, param_spec in kwargs.iteritems():
                par_id = self._find_parameter(param_name)
                if par_id is None:
                    raise ValueError("Cannot set parameter. `%s` not "
                                     "a valid ID or parameter name."
                                     % param_name)
                try:
                    # try to read both parameter value and error
                    param_val, param_err = param_spec
                except TypeError:
                    # if param_spec is not iterable, then only value
                    # was given
                    if not no_warning:
                        logger.warn("Parameter error not given for %s. "
                                    "Setting to 1/10th of the parameter "
                                    "value given." % (param_name,))
                    param_val, param_err = param_spec, param_spec * 0.1

                self.current_parameter_values[par_id] = param_val
                self.current_parameter_errors[par_id] = param_err

        # try to update the minimizer's parameters
        # (fails if minimizer not yet initialized)
        try:
            # set Minuit's start parameters and parameter errors
            self.minimizer.set_parameter_values(
                self.current_parameter_values)
            self.minimizer.set_parameter_errors(
                self.current_parameter_errors)
        except AttributeError:
            if not no_warning:
                logger.warn("Failed to set the minimizer's parameters. "
                            "Maybe minimizer not initialized for this Fit "
                            "yet?")

    def fix_parameters(self, *parameters_to_fix):
        '''
        Fix the given parameters so that the minimizer works without them
        when `do_fit` is called next. Parameters can be given by their names
        or by their IDs.
        '''
        for parameter in parameters_to_fix:
            # turn names into IDs, if needed
            par_id = self._find_parameter(parameter)
            if par_id is None:
                raise ValueError("Cannot fix parameter. `%s` not "
                                 "a valid ID or parameter name."
                                 % parameter)
            # found parameter, fix it
            self.minimizer.fix_parameter(par_id)
            self.number_of_fixed_parameters += 1
            self._fixed_parameters[par_id] = True
            logger.info("Fixed parameter %d (%s)"
                        % (par_id, self.parameter_names[par_id]))

    def release_parameters(self, *parameters_to_release):
        '''
        Release the given parameters so that the minimizer begins to work with
        them when `do_fit` is called next. Parameters can be given by their
        names or by their IDs. If no arguments are provied, then release all
        parameters.
        '''
        if parameters_to_release:
            for parameter in parameters_to_release:
                # turn names into IDs, if needed
                par_id = self._find_parameter(parameter)
                if par_id is None:
                    raise ValueError("Cannot release parameter. `%s` not "
                                     "a valid ID or parameter name."
                                     % parameter )

                # Release found parameter
                self.minimizer.release_parameter(par_id)
                self.number_of_fixed_parameters -= 1
                self._fixed_parameters[par_id] = False
                logger.info("Released parameter %d (%s)"
                            % (par_id, self.parameter_names[par_id]))
        else:
            # go through all parameter IDs
            for par_id in xrange(self.number_of_parameters):
                # Release parameter
                self.minimizer.release_parameter(par_id)

            # Inform about release
            logger.info("Released all parameters")

    def constrain_parameters(self, parameters, parvals, parerrs):
      r'''
      Constrain the parameter with the given name to :math:`c\pm\sigma`.

      This is achieved by adding an appropriate `penalty term` to the :math:`\chi^2`
      function, see function ``chi2``.


         **parameters** list of paramter id's or names to constrain

         **parvals**    list of parameter values

         **parerrs**    list of errors on parameters

      '''

      # turn name(s) into Id(s), if needed
      for i, parameter in enumerate(parameters):
        par_id = self._find_parameter(parameter)
        if par_id is None:
          raise ValueError("Cannot constrain parameter. `%s` not "
                           "a valid ID or parameter name."
                          % parameter )
        else:
        # constrain the parameter
          self.constrained_parameters[0][par_id]=list(parvals)[i]
          self.constrained_parameters[1][par_id]=list(parerrs)[i]
          self.number_of_constrained_parameters+=1
          self._constrained_parameters[par_id]=True

        logger.info("Constrained parameter %d (%s)"
                            % (par_id, self.parameter_names[par_id]))


    # Private Methods
    ##################

    def _find_parameter(self, lookup_string):
        '''
        Accepts a parameter's name or ID and returns its ID. If not found,
        returns ``None``.
        '''
        # Try to find the parameter by its ID
        try:
            self.current_parameter_values[lookup_string]
        except TypeError:
            # ID invalid. Try to lookup by name.
                try:
                    found_id = self.parameter_names.index(lookup_string)
                except ValueError:
                    return None
        else:
            found_id = lookup_string

        return found_id

    # Fit Workflow
    ###############

    def do_fit(self, quiet=False, verbose=False):
        '''
        Runs the fit algorithm for this `Fit` object.

        First, the `Dataset` is fitted considering only uncertainties in the
        `y` direction. If the `Dataset` has no uncertainties in the `y`
        direction, they are assumed to be equal to 1.0 for this preliminary
        fit, as there is no better information available.

        Next, the fit errors in the `x` direction (if they exist) are taken
        into account by projecting the covariance matrix for the `x` errors
        onto the `y` covariance matrix. This is done by taking the first
        derivative of the fit function in each point and "projecting" the `x`
        error onto the resulting tangent to the curve.

        This last step is repeated until the change in the error matrix caused
        by the projection becomes negligible.

        *quiet* : boolean (optional)
            Set to ``True`` if no output should be printed.

        *verbose* : boolean (optional)
            Set to ``True`` if more output should be printed.
        '''

        # insert timestamp
        self.out_stream.write_timestamp('Fit performed on')

        if not quiet:
            print >>self.out_stream, "###########"
            print >>self.out_stream, "# Dataset #"
            print >>self.out_stream, "###########"
            print >>self.out_stream, ''
            print >>self.out_stream, self.dataset.get_formatted()

            print >>self.out_stream, "################"
            print >>self.out_stream, "# Fit function #"
            print >>self.out_stream, "################"
            print >>self.out_stream, ''
            print >>self.out_stream, self.fit_function.get_function_equation(
                'ascii',
                'full' )
            print >>self.out_stream, ''

            if(self.number_of_constrained_parameters):
                print >>self.out_stream, "###############"
                print >>self.out_stream, "# Constraints #"
                print >>self.out_stream, "###############"
                print >>self.out_stream, ''
                for i, err in enumerate(self.constrained_parameters[1]):
                    if(err):
                        print >>self.out_stream, "%s: %g +\- %g" % (
                            self.parameter_names[i], self.constrained_parameters[0][i],
                                                  err )
                print >>self.out_stream, ''


        if not self.number_of_constrained_parameters:
            self.constrained_parameters = None  # avoid loop in function chi2

        max_x_iterations = 10

        logger.debug("Calling Minuit")
        if self.dataset.has_errors('x'):
            self.call_minimizer(final_fit=False, verbose=verbose)
        else:
            self.call_minimizer(final_fit=True, verbose=verbose)

        # if the dataset has x errors, project onto the current error matrix
        if self.dataset.has_errors('x'):
            logger.debug("Dataset has `x` errors. Iterating for `x` error.")
            iter_nr = 0
            while iter_nr < max_x_iterations:
                old_matrix = copy(self.current_cov_mat)
                self.project_x_covariance_matrix()

                logger.debug("`x` fit iteration %d" % (iter_nr,))
                if iter_nr==0:
                    self.call_minimizer(final_fit=False, verbose=verbose)
                else:
                    self.call_minimizer(final_fit=True, verbose=verbose)
                new_matrix = self.current_cov_mat

                # stop if the matrix has not changed within tolerance)
     # GQ: adjusted precision: rtol 1e-4 on cov-matrix is clearly sufficient
                if np.allclose(old_matrix, new_matrix, atol=0, rtol=1e-4):
                    logger.debug("Matrix for `x` fit iteration has converged.")
                    break   # interrupt iteration

                iter_nr += 1

        if not quiet:
            self.print_fit_results()
            self.print_rounded_fit_parameters()
            self.print_fit_details()

    def call_minimizer(self, final_fit=True, verbose=False):
        '''
        Instructs the minimizer to do a minimization.
        '''

        verbosity = 0
        if(final_fit):
            verbosity = 2
        if (verbose):
            verbosity = 3

        logger.debug("Calling minimizer")
        self.minimizer.minimize(
            final_fit=final_fit,log_print_level=verbosity)
        logger.debug("Retrieving data from minimizer")
        self.current_parameter_values = self.minimizer.get_parameter_values()
        self.current_parameter_errors = self.minimizer.get_parameter_errors()

    def project_x_covariance_matrix(self):
        r'''
        Project elements of the `x` covariance matrix onto the total
        matrix.

        This is done element-wise, according to the formula:

        .. math ::

            C_{\text{tot}, ij} = C_{y, ij} + C_{x, ij}
            \frac{\partial f}{\partial x_i}  \frac{\partial f}{\partial x_j}
        '''

        # Log projection (DEBUG)
        logger.debug("Projecting `x` covariance matrix.")

        # use 1/100th of the smallest error as spacing for df/dx
        precision_list = 0.01 * np.sqrt(np.diag(self.current_cov_mat) )

        if min(precision_list)==0:
            logger.warn('At least one input error is zero - set to 1e-7')
            for i, p in enumerate(precision_list):
                if not p:
                    precision_list[i] = 1.e-7

        outer_prod = outer_product(
            self.fit_function.derive_by_x(self.dataset.get_data('x'),
                                          precision_list,
                                          self.current_parameter_values)
        )

        proj_xcov_mat = np.asarray(self.dataset.get_cov_mat('x')) * outer_prod

        self.current_cov_mat = self.dataset.get_cov_mat('y') + \
            np.asmatrix(proj_xcov_mat)

    # Output functions
    ###################

    def print_rounded_fit_parameters(self):
        '''prints the fit parameters'''

        print >>self.out_stream, "########################"
        print >>self.out_stream, "# Final fit parameters #"
        print >>self.out_stream, "########################"
        print >>self.out_stream, ''

        for name, value, error in self.minimizer.get_parameter_info():

            tmp_rounded = round_to_significance(value, error, FORMAT_ERROR_SIGNIFICANT_PLACES)
            if error:
                print >>self.out_stream, "%s = %g +- %g" % (
                    name, tmp_rounded[0], tmp_rounded[1])
            else:
                print >>self.out_stream, "%s = %g    -fixed-" % (
                    name, tmp_rounded[0])

        print >>self.out_stream, ''

    def print_fit_details(self):
        '''prints some fit goodness details'''

        _ndf = (self.dataset.get_size() - self.number_of_parameters
               + self.number_of_fixed_parameters
               + self.number_of_constrained_parameters)


        chi2prob = self.minimizer.get_chi2_probability(_ndf)
        if chi2prob < F_SIGNIFICANCE_LEVEL:
            hypothesis_status = 'rejected (sig. %d%s)' \
                % (int(F_SIGNIFICANCE_LEVEL*100), '%')
        else:
            hypothesis_status = 'accepted (sig. %d%s)' \
                % (int(F_SIGNIFICANCE_LEVEL*100), '%')

        print >>self.out_stream, '###############'
        print >>self.out_stream, "# Fit details #"
        print >>self.out_stream, "###############"
        print >>self.out_stream, ''

        # Print a warning if NDF is zero
        if not _ndf:
            print >>self.out_stream, \
                  "# WARNING: Number of degrees of freedom is zero!"
            print >>self.out_stream, \
                  "# Please review parameterization..."
            print ''
        elif _ndf < 0:
            print >>self.out_stream, \
                  "# WARNING: Number of degrees of freedom is negative!"
            print >>self.out_stream, \
                  "# Please review parameterization..."
            print ''

        print >>self.out_stream, 'FCN      %g'\
            %(self.minimizer.get_fit_info('fcn'))
        if _ndf:
            print >>self.out_stream, 'FCN/ndf  %g'\
                %(self.minimizer.get_fit_info('fcn')/(_ndf))
        else:
            print >>self.out_stream, 'FCN/ndf ', "NaN"
        print >>self.out_stream, 'EdM      %g' \
            %(self.minimizer.get_fit_info('edm'))
        print >>self.out_stream, 'UP       %g' \
            %(self.minimizer.get_fit_info('err_def'))
        print >>self.out_stream, 'STA     ', \
            self.minimizer.get_fit_info('status_code')
        print >>self.out_stream, ''
        print >>self.out_stream, 'chi2prob', round(chi2prob, 3)
        print >>self.out_stream, 'HYPTEST ', hypothesis_status
        print >>self.out_stream, ''

    def print_fit_results(self):
        '''prints fit results'''

        print >>self.out_stream, '##############'
        print >>self.out_stream, '# Fit result #'
        print >>self.out_stream, '##############'
        print >>self.out_stream, ''

        par_cov_mat = self.get_error_matrix()
        par_err = extract_statistical_errors(par_cov_mat)
        par_cor_mat = MinuitCov_to_cor(par_cov_mat)


        print >>self.out_stream, '# value        error   ',
        if self.number_of_parameters > 1:
            print >>self.out_stream, 'correlations'
        else:
            print >>self.out_stream, ''
        for par_nr, par_val in enumerate(self.current_parameter_values):
            print >>self.out_stream, '# '+self.parameter_names[par_nr]
            print >>self.out_stream, format(par_val, '.04e')+'  ',
            if par_err[par_nr]:
              print >>self.out_stream, format(par_err[par_nr], '.02e')+'  ',
            else:
              print >>self.out_stream, '-fixed- ',
            if par_nr > 0 and par_err[par_nr]:
                for i in xrange(par_nr):
                    print >>self.out_stream, format(par_cor_mat[par_nr, i],
                                                    '.3f')+'  ',
            print >>self.out_stream, ''
        print >>self.out_stream, ''

def build_fit(dataset, fitfunc,
              fitlabel='untitled', initial_fit_parameters=None,
              constrained_parameters=None ) :
  '''
  This helper fuction creates a ``Fit`` from a series of keyword arguments.

  Valid keywords are:

  **dataset** : a `kafe` ``Dataset``

  **fitfunc** : a python function, eventually with
      ``@FitFunction``, ``@LATEX`` and ``@FitFunction`` decorators

  **fitlabel** : name for this fit

  **fitparameters** : None or 2-tuple of list, tuple/`np.array` of floats
      specifying initial parameter values and errors

  **constrained_parameters**: None or 3-tuple of list, tuple/np.array`
     of one string and 2 floats specifiying the names, values and
     uncertainties of constraints to apply to model parameters

  **returns** `Fit` object

  '''

# create a ``Fit`` object
  theFit = Fit(dataset,fitfunc,fit_label=fitlabel)
#  - set initial parameter values and range
  if(initial_fit_parameters is not None):
    theFit.set_parameters(initial_fit_parameters[0],      # values
                          initial_fit_parameters[1])      # ranges
#  - set parameter constraints
  if(constrained_parameters is not None):
    theFit.constrain_parameters( constrained_parameters[0], # names
                                 constrained_parameters[1], # val's
                                 constrained_parameters[2]) # uncert's
#
  return theFit
