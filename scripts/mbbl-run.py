#!/usr/bin/env python
"""Run PILCO on Model-Based Baseline Environments"""
import argparse
import logging
import os
import shutil
import sys

import numpy as np
import yaml

from pilco import get_env_info
from pilco import rl
from pilco import utils
from pilco.rl.agents import pilco

logger = logging.getLogger(__name__)

DTYPES = {"float32": np.float32, "float64": np.float64}


def parse_args(args=None):
    """Parse command-line arguments.

    Args:
        args: A list of argument strings to use instead of sys.argv.

    Returns:
        An `argparse.Namespace` object containing the parsed arguments.
    """
    parser = argparse.ArgumentParser(
        description=__doc__.splitlines()[0] if __doc__ else None,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )

    agent_parser = parser.add_argument_group("Agent")
    agent_parser.add_argument(
        "--policy-size",
        default=50,
        type=int,
        help="Number of units in the policy network.",
    )
    agent_parser.add_argument(
        "--max-horizon", type=int, help="Maximum horizon for policy evaluation."
    )
    agent_parser.add_argument(
        "--initial-horizon",
        type=int,
        default=30,
        help="Initial horizon for dynamic horizon.",
    )
    agent_parser.add_argument(
        "--transient-horizon",
        type=int,
        default=0,
        help="Exclude this many initial steps when calculating reward threshold.",
    )
    agent_parser.add_argument(
        "--expand-horizon-threshold",
        type=float,
        default=0.5,
        help="Expand the horizon when the average reward exceeds this threshold.",
    )
    agent_parser.add_argument(
        "--expand-horizon-factor",
        type=float,
        default=2,
        help="Increase horizon by this factor when solved current horizon.",
    )
    agent_parser.add_argument(
        "--policy-update-iterations",
        default=50,
        type=int,
        help="Maximum number of policy update iterations between episodes.",
    )
    agent_parser.add_argument(
        "--dynamics-update-iterations",
        default=1000,
        type=int,
        help="Maximium number of dynamics model update iterations (where applicable).",
    )
    agent_parser.add_argument(
        "--gpu", nargs="?", const=0, metavar="INDEX", help="Use the GPU for training."
    )
    agent_parser.add_argument(
        "--dynamics-model",
        choices=DYNAMICS_REGRESSORS,
        default="sgpr",
        action=utils.cli.DictLookupAction,
        help="GP Regressor used as the dynamics model.",
    )
    agent_parser.add_argument(
        "--min-noise",
        default=1e-8,
        type=float,
        help="Minimum dynamics model noise. Has a regularizing effect.",
    )
    agent_parser.add_argument(
        "--no-shared-kernel",
        action="store_false",
        dest="shared_kernel",
        help="Dynamics model dimensions use independent kernels."
        "Cannot be used with sgpr.",
    )
    agent_parser.add_argument(
        "--num-inducing-points",
        default=200,
        type=int,
        help="Number of inducing points for a sparse GP dynamics model.",
    )
    agent_parser.add_argument(
        "--random-actions",
        action="store_true",
        help="First episode actions are uniform random.",
    )
    agent_parser.add_argument(
        "--history-buffer-size", type=int, help="Maximum history buffer size."
    )
    agent_parser.add_argument(
        "--dtype",
        action=utils.cli.DictLookupAction,
        choices=DTYPES,
        default="float64",
        help="Data type to use.",
    )

    env_parser = parser.add_argument_group("Environment")
    env_parser.add_argument(
        "--env", default="gym_swimmer", type=str, help="Environment to run."
    )
    env_parser.add_argument(
        "--env-seed", default=0, type=int, help="Environment random seed."
    )
    env_parser.add_argument(
        "--num_maxsteps", default=200000, type=int, help="How many episodes to run?."
    )

    logging_parser = parser.add_argument_group("Logging")
    logging_parser.add_argument(
        "--log",
        type=str,
        nargs="?",
        const="",
        metavar="LOGDIR",
        help="Log results. If %(metavar)s is given, log to that directory "
        "overriding ROOT_LOGDIR.",
    )
    logging_parser.add_argument(
        "--log-all",
        action="store_true",
        help="Generate all logs including those that may slow training.",
    )
    logging_parser.add_argument(
        "--root-logdir",
        type=str,
        default=os.path.join(os.path.expanduser("~"), "data", "pilco"),
        help="Root log directory. A subdirectory named by the current time is "
        "created for this run.",
    )
    logging_parser.add_argument(
        "--log-level",
        default="info",
        type=str,
        choices=("debug", "info", "warn", "error"),
        help="Set the console log level for first-party loggers.",
    )
    logging_parser.add_argument(
        "--all-log-level",
        type=str,
        choices=("debug", "info", "warn", "error"),
        help="Set the console log level for all loggers.",
    )
    logging_parser.add_argument(
        "-V",
        "--visualize",
        action="store_true",
        help="Visualize the model predictions.",
    )

    return parser.parse_args(args)


def _get_gpr_sklearn(shared_kernel, min_noise=1e-10, **kwargs):
    """Sklearn gaussian process regressor."""
    del kwargs
    import pilco.gp.sklearn

    return pilco.gp.sklearn.SklearnRBFGaussianProcessRegressor(
        shared_kernel=shared_kernel, noise_variance_bounds=(min_noise, 1e5)
    )


def _get_gpr(shared_kernel, max_iterations, min_noise, **kwargs):
    """Gpflow gaussian process regressor."""
    del kwargs
    import pilco.gp.gpflow

    return pilco.gp.gpflow.GpflowRBFGaussianProcessRegressor(
        shared_kernel=shared_kernel,
        max_iterations=max_iterations,
        min_noise_variance=min_noise,
    )


def _get_sgpr(shared_kernel, num_inducing_points, max_iterations, min_noise, **kwargs):
    """Gpflow gaussian process regressor."""
    del kwargs
    import pilco.gp.gpflow

    return pilco.gp.gpflow.GpflowRBFSparseVariationalGaussianProcessRegressor(
        shared_kernel=shared_kernel,
        num_inducing_points=num_inducing_points,
        max_iterations=max_iterations,
        min_noise_variance=min_noise,
    )


DYNAMICS_REGRESSORS = {
    "gpr-sklearn": _get_gpr_sklearn,
    "gpr": _get_gpr,
    "sgpr": _get_sgpr,
}


def main(args=None):
    """Run script.

    Args:
        args: A list of argument strings to use instead of sys.argv.
    """
    args = parse_args(args)
    if args.all_log_level is not None:
        logging.getLogger(None).setLevel(args.all_log_level.upper())
    logger.setLevel(args.log_level.upper())
    logging.getLogger(name="pilco").setLevel(args.log_level.upper())
    if args.log is not None:
        if args.log:
            log_dir = args.log
        else:
            log_dir = os.path.join(args.root_logdir, utils.cli.filename_datetime())
        os.makedirs(log_dir, exist_ok=True)
        logger.addHandler(logging.FileHandler(os.path.join(log_dir, "output.log")))
    else:
        log_dir = None

    (
        env,
        env_info,
        initial_state_mean,
        initial_state_covariance,
        reward_moment_map,
    ) = get_env_info.get_env_info(args.env, args.env_seed, args.dtype)

    if log_dir is not None:
        logger.info("Logging to: %s", log_dir)
        with open(os.path.join(log_dir, "args.yaml"), "w") as f:
            f.write(
                yaml.dump(
                    {"argv": sys.argv, "args": vars(args)}, default_flow_style=False
                )
            )

    dtype = args.dtype
    maximum_horizon = args.max_horizon
    if maximum_horizon is None:
        maximum_horizon = env_info["max_length"]
    maximum_horizon = min(env_info["max_length"], maximum_horizon)

    horizon = maximum_horizon

    dynamics_model = args.dynamics_model(
        shared_kernel=args.shared_kernel,
        min_noise=args.min_noise,
        num_inducing_points=args.num_inducing_points,
        max_iterations=args.dynamics_update_iterations,
    )

    agent = pilco.PILCOAgent(
        env=env,
        horizon=horizon,
        policy_size=args.policy_size,
        policy_update_iterations=args.policy_update_iterations,
        dynamics_regressor=dynamics_model,
        initial_random_actions=args.random_actions,
        max_history_buffer_size=args.history_buffer_size,
        full_logging=args.log_all,
        visualize=args.visualize,
        device=(f"/device:GPU:{args.gpu}" if args.gpu is not None else "/cpu:0"),
        log_dir=log_dir,
        reward_moment_map_fn=reward_moment_map,
        initial_state_mean=initial_state_mean,
        initial_state_covariance=initial_state_covariance,
        dtype=dtype,
    )

    i_step = 0
    for _ in rl.run.run_steps(env, agent):
        i_step += 1
        if i_step > args.num_maxsteps:
            break


if __name__ == "__main__":
    try:
        _np = sys.modules["numpy"]
    except KeyError:
        pass
    else:
        _np.set_printoptions(linewidth=shutil.get_terminal_size().columns)
    logging.basicConfig()
    main()
