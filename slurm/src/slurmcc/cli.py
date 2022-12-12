# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License.
#
import argparse
import logging
import os
import shutil
from subprocess import SubprocessError, check_output
import sys
import traceback
import time
from argparse import ArgumentParser
from math import ceil
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, TextIO, Union

from hpc.autoscale.cli import GenericDriver
from hpc.autoscale.clilib import (
    CommonCLI,
    ShellDict,
    disablecommand,
    main as clilibmain,
)
from hpc.autoscale import hpctypes as ht
from hpc.autoscale.hpctypes import Memory
from hpc.autoscale import util as hpcutil
from hpc.autoscale.job.demandprinter import OutputFormat
from hpc.autoscale.job.driver import SchedulerDriver
from hpc.autoscale.node.bucket import NodeBucket
from hpc.autoscale.node.delayednodeid import DelayedNodeId

from hpc.autoscale.node.nodemanager import NodeManager
from hpc.autoscale.node.node import Node
from hpc.autoscale.job.schedulernode import SchedulerNode

from . import partition as partitionlib
from . import util as slutil
from . import CyclecloudSlurmError
from hpc.autoscale.results import AllocationResult


VERSION = "3.0.0"


def csv_list(x: str) -> List[str]:
    # used in argument parsing
    return [x.strip() for x in x.split(",")]


def init_power_saving_log(function: Callable) -> Callable:
    def wrapped(*args: Any, **kwargs: Any) -> Any:
        root_logger = logging.getLogger()
        for handler in root_logger.handlers:
            if hasattr(handler, "baseFilename"):
                fname = getattr(handler, "baseFilename")
                if fname and fname.endswith(f"{function.__name__}.log"):
                    handler.setLevel(logging.INFO)
                    logging.info(f"initialized {function.__name__}.log")
        return function(*args, **kwargs)

    wrapped.__doc__ = function.__doc__
    return wrapped


class EasyNode(Node):
    def __init__(
        self,
        name: ht.NodeName,
        node_id: Optional[DelayedNodeId] = None,
        nodearray: ht.NodeArrayName = ht.NodeArrayName("execute"),
        bucket_id: Optional[ht.BucketId] = None,
        hostname: Optional[ht.Hostname] = None,
        private_ip: Optional[ht.IpAddress] = None,
        instance_id: Optional[ht.InstanceId] = None,
        vm_size: ht.VMSize = ht.VMSize("Standard_F4"),
        location: ht.Location = ht.Location("westus"),
        spot: bool = False,
        vcpu_count: int = 4,
        memory: ht.Memory = ht.Memory.value_of("8g"),
        infiniband: bool = False,
        state: ht.NodeStatus = ht.NodeStatus("Off"),
        target_state: ht.NodeStatus = ht.NodeStatus("Off"),
        power_state: ht.NodeStatus = ht.NodeStatus("off"),
        exists: bool = False,
        placement_group: Optional[ht.PlacementGroup] = None,
        managed: bool = True,
        resources: ht.ResourceDict = ht.ResourceDict({}),
        software_configuration: dict = {},
        keep_alive: bool = False,
        gpu_count: Optional[int] = None,
    ) -> None:

        Node.__init__(
            self,
            name=name,
            nodearray=nodearray,
            hostname=hostname,
            node_id=node_id or DelayedNodeId(name),
            bucket_id=bucket_id or "b1",
            private_ip=private_ip,
            instance_id=instance_id,
            vm_size=vm_size,
            location=location,
            spot=spot,
            vcpu_count=vcpu_count,
            memory=memory,
            infiniband=infiniband,
            state=state,
            target_state=target_state,
            power_state=power_state,
            exists=exists,
            placement_group=placement_group,
            managed=managed,
            resources=resources,
            software_configuration=software_configuration,
            keep_alive=keep_alive,
            gpu_count=gpu_count,
        )


class SlurmDriver(GenericDriver):
    def __init__(self) -> None:
        super().__init__("slurm")

    def preprocess_node_mgr(self, config: Dict, node_mgr: NodeManager) -> None:
        def default_dampened_memory(node: Node) -> Memory:
            return min(node.memory - Memory.value_of("1g"), node.memory * 0.95)

        node_mgr.add_default_resource(
            selection={},
            resource_name="slurm_memory",
            default_value=default_dampened_memory,
        )

        for b in node_mgr.get_buckets():
            if "nodearrays" not in config:
                config["nodearrays"] = {}
            if b.nodearray not in config["nodearrays"]:
                config["nodearrays"][b.nodearray] = {}
            # TODO remove
            config["nodearrays"][b.nodearray]["generated_placement_group_buffer"] = 0
            if "generated_placement_group_buffer" in config["nodearrays"][b.nodearray]:
                continue
            is_hpc = (
                str(
                    b.software_configuration.get("slurm", {}).get("hpc") or "false"
                ).lower()
                == "true"
            )
            if is_hpc:
                buffer = ceil(b.limits.max_count / b.max_placement_group_size)
            else:
                buffer = 0
            config["nodearrays"][b.nodearray][
                "generated_placement_group_buffer"
            ] = buffer
        # super().preprocess_node_mgr(config, node_mgr)


class SlurmCLI(CommonCLI):
    def __init__(self) -> None:
        super().__init__(project_name="slurm")
        self.slurm_node_names = []

    def _add_completion_data(self, completion_json: Dict) -> None:
        node_names = slutil.check_output(["sinfo", "-N", "-h", "-o", "%N"]).splitlines(
            keepends=False
        )
        node_lists = slutil.check_output(["sinfo", "-h", "-o", "%N"]).strip().split(",")
        completion_json["slurm_node_names"] = node_names + node_lists

    def _read_completion_data(self, completion_json: Dict) -> None:
        self.slurm_node_names = completion_json.get("slurm_node_names", [])

    def _slurm_node_name_completer(
        self,
        prefix: str,
        action: argparse.Action,
        parser: ArgumentParser,
        parsed_args: argparse.Namespace,
    ) -> List[str]:
        self._get_example_nodes(parsed_args.config)
        output_prefix = ""
        if prefix.endswith(","):
            output_prefix = prefix
        return [output_prefix + x + "," for x in self.slurm_node_names]

    def partitions_parser(self, parser: ArgumentParser) -> None:
        parser.add_argument("--allow-empty", action="store_true", default=False)

    def partitions(self, config: Dict, allow_empty: bool = False) -> None:
        """
        Generates partition configuration
        """
        node_mgr = self._get_node_manager(config)
        partitions = partitionlib.fetch_partitions(node_mgr)  # type: ignore
        _partitions(
            partitions,
            sys.stdout,
            allow_empty=allow_empty,
            autoscale=config.get("autoscale", True),
        )

    def generate_topology(self, config: Dict) -> None:
        """
        Generates topology plugin configuration
        """
        return _generate_topology(self._get_node_manager(config), sys.stdout)

    def resume_parser(self, parser: ArgumentParser) -> None:
        parser.set_defaults(read_only=False)
        parser.add_argument(
            "--node-list", type=hostlist, required=True
        ).completer = self._slurm_node_name_completer  # type: ignore
        parser.add_argument("--no-wait", action="store_true", default=False)

    @init_power_saving_log
    def resume(self, config: Dict, node_list: List[str], no_wait: bool = False) -> None:
        """
        Equivalent to ResumeProgram, starts and waits for a set of nodes.
        """
        node_mgr = self._get_node_manager(config)
        partitions = partitionlib.fetch_partitions(node_mgr)
        return self._resume(config, node_mgr, node_list, partitions, no_wait)

    def _resume(
        self,
        config: Dict,
        node_mgr: NodeManager,
        node_list: List[str],
        partitions: Dict[str, partitionlib.Partition],
        no_wait: bool = False,
    ) -> None:
        name_to_partition = {}
        for partition in partitions.values():
            for name in partition.all_nodes():
                name_to_partition[name] = partition
        existing_nodes_by_name = hpcutil.partition(
            node_mgr.get_nodes(), lambda n: n.name
        )

        nodes = []
        for name in node_list:
            if name in existing_nodes_by_name:
                logging.info(f"{name} already exists.")
                continue

            if name not in name_to_partition:
                raise CyclecloudSlurmError(
                    f"Unknown node name: {name}: {list(name_to_partition.keys())}"
                )
            partition = name_to_partition[name]
            bucket = partition.bucket_for_node(name)

            def name_hook(bucket: NodeBucket, index: int) -> str:
                if index != 1:
                    raise RuntimeError(f"Unexpected index: {index}")
                return name

            node_mgr.set_node_name_hook(name_hook)
            result: AllocationResult = node_mgr.allocate(
                {"node.bucket_id": bucket.bucket_id, "exclusive": True}, node_count=1
            )
            if len(result.nodes) != 1:
                raise RuntimeError()
            result.nodes[0].name_format = name
            nodes.extend(result.nodes)
        boot_result = node_mgr.bootup(nodes)

        if not no_wait:
            self._wait_for_resume(config, boot_result.operation_id, node_list)

    def wait_for_resume_parser(self, parser: ArgumentParser) -> None:
        parser.set_defaults(read_only=False)
        parser.add_argument(
            "--node-list", type=hostlist, required=True
        ).completer = self._slurm_node_name_completer  # type: ignore

    def wait_for_resume(self, config: Dict, node_list: List[str]) -> None:
        """
        Wait for a set of nodes to converge.
        """
        self._wait_for_resume(config, "noop", node_list)

    def _shutdown(self, node_list: List[str], node_mgr: NodeManager) -> None:
        # by_name = hpcutil.partition_single(node_mgr.get_nodes(), lambda node: node.name)
        # node_list_filtered = []
        # for node_name in node_list:
        #     if node_name in by_name:
        #         node_list_filtered.append(node_name)
        #     else:
        #         logging.info(f"{node_name} does not exist. Skipping.")
        #         node_list_filtered.append(node_name)
        # nodes = _as_nodes(node_list_filtered, node_mgr)

        nodes = []
        for n in node_list:
            nodes.append(EasyNode(n))
        result = node_mgr.shutdown_nodes(nodes)
        logging.info(str(result))

    def suspend_parser(self, parser: ArgumentParser) -> None:
        parser.set_defaults(read_only=False)
        parser.add_argument(
            "--node-list", type=hostlist, required=True
        ).completer = self._slurm_node_name_completer  # type: ignore

    @init_power_saving_log
    def suspend(self, config: Dict, node_list: List[str]) -> None:
        """
        Equivalent to SuspendProgram, shutsdown nodes
        """
        return self._shutdown(node_list, self._node_mgr(config))

    def resume_fail_parser(self, parser: ArgumentParser) -> None:
        self.suspend_parser(parser)

    @init_power_saving_log
    def resume_fail(
        self, config: Dict, node_list: List[str], drain_timeout: int = 300
    ) -> None:
        """
        Equivalent to SuspendFailProgram, shutsdown nodes
        """
        node_mgr = self._node_mgr(config, self._driver(config))
        self._shutdown(node_list=node_list, node_mgr=node_mgr)

    def _get_node_manager(self, config: Dict, force: bool = False) -> NodeManager:
        return self._node_mgr(config, self._driver(config), force=force)

    def _setup_shell_locals(self, config: Dict) -> Dict:
        # TODO
        shell = {}
        partitions = partitionlib.fetch_partitions(self._get_node_manager(config))  # type: ignore
        shell["partitions"] = ShellDict(partitions)
        shell["node_mgr"] = node_mgr = self._get_node_manager(config)
        nodes = {}

        for node in node_mgr.get_nodes():
            node.shellify()
            nodes[node.name] = node
            if node.hostname:
                nodes[node.hostname] = node
        shell["nodes"] = ShellDict(nodes)

        def slurmhelp() -> None:
            def _print(key: str, desc: str) -> None:
                print("%-20s %s" % (key, desc))

            _print("partitions", "partition information")
            _print("node_mgr", "NodeManager")
            _print(
                "nodes",
                "Current nodes according to the provider. May include nodes that have not joined yet.",
            )

        shell["slurmhelp"] = slurmhelp
        return shell

    def _driver(self, config: Dict) -> SchedulerDriver:
        return SlurmDriver()

    def _default_output_columns(
        self, config: Dict, cmd: Optional[str] = None
    ) -> List[str]:
        if hpcutil.LEGACY:
            return ["nodearray", "name", "hostname", "private_ip", "status"]
        return ["pool", "name", "hostname", "private_ip", "status"]

    def _initconfig_parser(self, parser: ArgumentParser) -> None:
        # TODO
        parser.add_argument("--accounting-tag-name", dest="accounting__tag_name")
        parser.add_argument("--accounting-tag-value", dest="accounting__tag_value")
        parser.add_argument(
            "--accounting-subscription-id", dest="accounting__subscription_id"
        )

    def _initconfig(self, config: Dict) -> None:
        import json
        with open("/sched/demo.json") as fr:
            config.update(json.load(fr))
        

    @disablecommand
    def analyze(self, config: Dict, job_id: str, long: bool = False) -> None:
        ...

    @disablecommand
    def validate_constraint(
        self,
        config: Dict,
        constraint_expr: List[str],
        writer: TextIO = sys.stdout,
        quiet: bool = False,
    ) -> Union[List, Dict]:
        return super().validate_constraint(
            config, constraint_expr, writer=writer, quiet=quiet
        )

    @disablecommand
    def join_nodes(
        self, config: Dict, hostnames: List[str], node_names: List[str]
    ) -> None:
        return super().join_nodes(config, hostnames, node_names)

    @disablecommand
    def jobs(self, config: Dict) -> None:
        return super().jobs(config)

    @disablecommand
    def demand(
        self,
        config: Dict,
        output_columns: Optional[List[str]],
        output_format: OutputFormat,
        long: bool = False,
    ) -> None:
        return super().demand(config, output_columns, output_format, long=long)

    @disablecommand
    def autoscale(
        self,
        config: Dict,
        output_columns: Optional[List[str]],
        output_format: OutputFormat,
        dry_run: bool = False,
        long: bool = False,
    ) -> None:
        return super().autoscale(
            config, output_columns, output_format, dry_run=dry_run, long=long
        )

    def scale_parser(self, parser: ArgumentParser) -> None:
        return

    def scale(
        self,
        config: Dict,
        backup_dir="/etc/slurm/.backups",
        slurm_conf_dir="/etc/slurm",
        sched_dir="/sched",
        config_only=False,
    ):
        node_mgr = self._get_node_manager(config)
        # make sure .backups exists
        now = time.time()
        backup_dir = os.path.join(backup_dir, str(now))

        logging.debug(
            "Using backup directory %s for azure.conf and gres.conf", backup_dir
        )
        os.makedirs(backup_dir)

        azure_conf = os.path.join(sched_dir, "azure.conf")
        gres_conf = os.path.join(slurm_conf_dir, "gres.conf")

        if os.path.exists(azure_conf):
            shutil.copyfile(azure_conf, os.path.join(backup_dir, "azure.conf"))
        
        if os.path.exists(gres_conf):
            shutil.copyfile(gres_conf, os.path.join(backup_dir, "gres.conf"))

        partition_dict = partitionlib.fetch_partitions(node_mgr)
        with open(azure_conf + ".tmp", "w") as fw:
            _partitions(
                partition_dict,
                fw,
                allow_empty=False,
                autoscale=config.get("autoscale", True),
            )

        logging.debug(
            "Moving %s to %s", azure_conf + ".tmp", azure_conf
        )
        shutil.move(azure_conf + ".tmp", azure_conf)

        _update_future_states(node_mgr)

        with open(gres_conf + ".tmp", "w") as fw:
            _generate_gres_conf(partition_dict, fw)
        shutil.move(gres_conf + ".tmp", gres_conf)

        logging.info("Restarting slurmctld...")
        check_output(["systemctl", "restart", "slurmctld"])

        logging.info("")
        logging.info("Re-scaling cluster complete.")

    def keep_alive_parser(self, parser: ArgumentParser) -> None:
        parser.set_defaults(read_only=False)
        parser.add_argument(
            "--node-list", type=hostlist, required=True
        ).completer = self._slurm_node_name_completer  # type: ignore

        parser.add_argument("--remove", "-r", action="store_true", default=False)
        parser.add_argument(
            "--set", "-s", action="store_true", default=False, dest="set_nodes"
        )

    def keep_alive(
        self,
        config: Dict,
        node_list: List[str],
        remove: bool = False,
        set_nodes: bool = False,
    ) -> None:
        """
        Add, remeove or set which nodes should be prevented from being shutdown.

        """
        if remove and set_nodes:
            raise CyclecloudSlurmError(
                "Please define only --set or --remove, not both."
            )

        lines = slutil.check_output(["scontrol", "show", "config"]).splitlines()
        filtered = [
            line for line in lines if line.lower().startswith("suspendexcnodes")
        ]
        current_susp_nodes = []
        if filtered:
            current_susp_nodes_expr = filtered[0].split("=")[-1].strip()
            if current_susp_nodes_expr != "(null)":
                current_susp_nodes = slutil.from_hostlist(current_susp_nodes_expr)

        if set_nodes:
            hostnames = list(set(node_list))
        elif remove:
            hostnames = list(set(current_susp_nodes) - set(node_list))
        else:
            hostnames = current_susp_nodes + node_list

        all_susp_hostnames = (
            slutil.check_output(
                [
                    "scontrol",
                    "show",
                    "hostnames",
                    ",".join(hostnames),
                ]
            )
            .strip()
            .split()
        )
        all_susp_hostnames = sorted(
            list(set(all_susp_hostnames)), key=slutil.get_sort_key_func(False)
        )
        all_susp_hostlist = slutil.check_output(
            ["scontrol", "show", "hostlist", ",".join(all_susp_hostnames)]
        ).strip()

        with open("/sched/keep_alive.conf.tmp", "w") as fw:
            fw.write(f"SuspendExcNodes = {all_susp_hostlist}")
        shutil.move("/sched/keep_alive.conf.tmp", "/sched/keep_alive.conf")
        slutil.check_output(["scontrol", "reconfig"])

    def _wait_for_resume(
        self,
        config: Dict,
        operation_id: str,
        node_list: List[str],
    ) -> None:
        previous_states = {}

        nodes_str = ",".join(node_list[:5])
        omega = time.time() + 3600

        failed_node_names: Set[str] = set()

        ready_nodes: List[Node] = []

        while time.time() < omega:
            ready_nodes = []
            states = {}

            node_mgr = self._get_node_manager(config, force=True)
            nodes = _retry_rest(lambda: node_mgr.get_nodes())

            by_name = hpcutil.partition_single(nodes, lambda node: node.name)

            relevant_nodes: List[Node] = []

            recovered_node_names: Set[str] = set()

            newly_failed_node_names: List[str] = []

            deleted_nodes = []

            for name in node_list:
                node = by_name.get(name)
                if not node:
                    deleted_nodes.append(node)
                    continue

                relevant_nodes.append(node)

                state = node.state

                if state and state.lower() == "failed":
                    states["Failed"] = states.get("Failed", 0) + 1
                    if name not in failed_node_names:
                        newly_failed_node_names.append(name)
                        failed_node_names.add(name)

                    continue

                if name in failed_node_names:
                    recovered_node_names.add(name)

                if node.target_state != "Started":
                    states["UNKNOWN"] = states.get("UNKNOWN", {})
                    states["UNKNOWN"][node.state] = states["UNKNOWN"].get(state, 0) + 1
                    continue

                if node.state == "Ready":
                    if not node.private_ip:
                        state = "WaitingOnIPAddress"
                    else:
                        ready_nodes.append(node)

                states[state] = states.get(state, 0) + 1

            if newly_failed_node_names:
                failed_node_names_str = ",".join(failed_node_names)
                try:
                    logging.error(
                        "The following nodes failed to start: %s", failed_node_names_str
                    )
                    for failed_name in failed_node_names:
                        cmd = [
                            "scontrol",
                            "update",
                            "NodeName=%s" % failed_name,
                            "State=down",
                            "Reason=cyclecloud_node_failure",
                        ]
                        logging.info("Running %s", " ".join(cmd))
                        slutil.check_output(cmd)
                except Exception:
                    logging.exception(
                        "Failed to mark the following nodes as down: %s. Will re-attempt next iteration.",
                        failed_node_names_str,
                    )

            if recovered_node_names:
                recovered_node_names_str = ",".join(recovered_node_names)
                try:
                    for recovered_name in recovered_node_names:
                        logging.error(
                            "The following nodes have recovered from failure: %s",
                            recovered_node_names_str,
                        )
                        cmd = [
                            "scontrol",
                            "update",
                            "NodeName=%s" % recovered_name,
                            "State=idle",
                            "Reason=cyclecloud_node_recovery",
                        ]
                        logging.info("Running %s", " ".join(cmd))
                        slutil.check_output(cmd)
                        if recovered_name in failed_node_names:
                            failed_node_names.pop(recovered_name)
                except Exception:
                    logging.exception(
                        "Failed to mark the following nodes as recovered: %s. Will re-attempt next iteration.",
                        recovered_node_names_str,
                    )

            terminal_states = (
                states.get("Ready", 0)
                + sum(states.get("UNKNOWN", {}).values())
                + states.get("Failed", 0)
            )

            if states != previous_states:
                states_messages = []
                for key in sorted(states.keys()):
                    if key != "UNKNOWN":
                        states_messages.append("{}={}".format(key, states[key]))
                    else:
                        for ukey in sorted(states["UNKNOWN"].keys()):
                            states_messages.append(
                                "{}={}".format(ukey, states["UNKNOWN"][ukey])
                            )

                states_message = " , ".join(states_messages)
                logging.info(
                    "OperationId=%s NodeList=%s: Number of nodes in each state: %s",
                    operation_id,
                    nodes_str,
                    states_message,
                )

            if terminal_states == len(relevant_nodes):
                break

            previous_states = states

            time.sleep(5)

        logging.info(
            "The following nodes reached Ready state: %s",
            ",".join([x.name for x in ready_nodes]),
        )
        for node in ready_nodes:
            if not hpcutil.is_valid_hostname(config, node):
                continue
            cmd = [
                "scontrol",
                "update",
                "NodeName=%s" % node.name,
                "NodeAddr=%s" % node.private_ip,
                "NodeHostName=%s" % node.hostname,
            ]
            logging.info("Running %s", " ".join(cmd))
            slutil.check_output(cmd)

        logging.info(
            "OperationId=%s NodeList=%s: all nodes updated with the proper IP address. Exiting",
            operation_id,
            nodes_str,
        )


def _partitions(
    partitions: Dict[str, partitionlib.Partition],
    writer: TextIO,
    allow_empty: bool = False,
    autoscale: bool = True,
) -> None:
    for partition in partitions.values():
        node_list = partition.node_list or []

        max_count = min(partition.max_vm_count, partition.max_scaleset_size)
        default_yn = "YES" if partition.is_default else "NO"

        memory = max(1024, partition.memory)
        def_mem_per_cpu = memory // partition.pcpu_count

        if partition.use_pcpu:
            cpus = partition.pcpu_count
            # cores_per_socket = 1
        else:
            cpus = partition.vcpu_count
            # cores_per_socket = max(1, partition.vcpu_count // partition.pcpu_count)

        writer.write(
            "# Note: CycleCloud reported a RealMemory of %d but we reduced it by %d (i.e. max(1gb, %d%%)) to account for OS/VM overhead which\n"
            % (
                int(partition.memory * 1024),
                -1,
                -1,
                # int(partition.dampen_memory * 100),
            )
        )
        writer.write(
            "# would result in the nodes being rejected by Slurm if they report a number less than defined here.\n"
        )
        writer.write(
            "# To pick a different percentage to dampen, set slurm.dampen_memory=X in the nodearray's Configuration where X is percentage (5 = 5%).\n"
        )
        writer.write(
            "PartitionName={} Nodes={} Default={} DefMemPerCPU={} MaxTime=INFINITE State=UP\n".format(
                partition.name, partition.node_list, default_yn, def_mem_per_cpu
            )
        )

        if partition.use_pcpu:
            cpus = partition.pcpu_count
            threads = max(1, partition.vcpu_count // partition.pcpu_count)
        else:
            cpus = partition.vcpu_count
            threads = 1
        state = "CLOUD" if autoscale else "FUTURE"
        writer.write(
            "Nodename={} Feature=cloud STATE={} CPUs={} ThreadsPerCore={} RealMemory={}".format(
                node_list, state, cpus, threads, memory
            )
        )

        if partition.gpu_count:
            writer.write(" Gres=gpu:{}".format(partition.gpu_count))

        writer.write("\n")


def _generate_topology(node_mgr: NodeManager, writer: TextIO) -> None:
    partitions = partitionlib.fetch_partitions(node_mgr)

    nodes_by_pg = {}
    for partition in partitions.values():
        for pg, node_list in partition.node_list_by_pg.items():
            if pg not in nodes_by_pg:
                nodes_by_pg[pg] = []
            nodes_by_pg[pg].extend(node_list)

    if not nodes_by_pg:
        raise CyclecloudSlurmError(
            "No nodes found to create topology! Do you need to run create_nodes first?"
        )

    for pg in sorted(nodes_by_pg.keys(), key=lambda x: x if x is not None else ""):
        nodes = nodes_by_pg[pg]
        if not nodes:
            continue
        nodes = sorted(nodes, key=slutil.get_sort_key_func(bool(pg)))
        slurm_node_expr = ",".join(nodes)  # slutil.to_hostlist(",".join(nodes))
        writer.write("SwitchName={} Nodes={}\n".format(pg or "htc", slurm_node_expr))


def _generate_gres_conf(partitions: Dict[str, partitionlib.Partition], writer: TextIO):
    for partition in partitions.values():
        if partition.node_list is None:
            raise RuntimeError(
                "No nodes found for nodearray %s. Please run 'cyclecloud_slurm.sh create_nodes' first!"
                % partition.nodearray
            )

        num_placement_groups = int(
            ceil(float(partition.max_vm_count) / partition.max_scaleset_size)
        )
        all_nodes = sorted(
            slutil.from_hostlist(partition.node_list),
            key=slutil.get_sort_key_func(partition.is_hpc),
        )

        for pg_index in range(num_placement_groups):
            start = pg_index * partition.max_scaleset_size
            end = min(
                partition.max_vm_count, (pg_index + 1) * partition.max_scaleset_size
            )
            subset_of_nodes = all_nodes[start:end]
            node_list = slutil.to_hostlist(",".join((subset_of_nodes)))
            # cut out 1gb so that the node reports at least this amount of memory. - recommended by schedmd

            if partition.gpu_count:
                if partition.gpu_count > 1:
                    nvidia_devices = "/dev/nvidia[0-{}]".format(partition.gpu_count - 1)
                else:
                    nvidia_devices = "/dev/nvidia0"
                writer.write(
                    "Nodename={} Name=gpu Count={} File={}".format(
                        node_list, partition.gpu_count, nvidia_devices
                    )
                )

            writer.write("\n")


def _update_future_states(node_mgr: NodeManager):
    autoscale_enabled = is_autoscale_enabled()
    if autoscale_enabled:
        return
    nodes = node_mgr.get_nodes()

    for node in nodes:
        if node.target_state != "Started":
            name = node.name
            try:
                cmd = [
                    "scontrol",
                    "update",
                    f"NodeName={name}",
                    f"NodeAddr={name}",
                    f"NodeHostName={name}",
                    "state=FUTURE",
                ]
                check_output(cmd)
            except SubprocessError:
                logging.warning(f"Could not set {node.get('Name')} state=FUTURE")


def _retry_rest(func: Callable, attempts: int = 5) -> Any:
    attempts = max(1, attempts)
    last_exception = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e
            logging.debug(traceback.format_exc())

            time.sleep(attempt * attempt)

    raise CyclecloudSlurmError(str(last_exception))


def _retry_subprocess(func: Callable, attempts: int = 5) -> Any:
    attempts = max(1, attempts)
    last_exception: Optional[Exception] = None
    for attempt in range(1, attempts + 1):
        try:
            return func()
        except Exception as e:
            last_exception = e
            logging.debug(traceback.format_exc())
            logging.warning("Command failed, retrying: %s", str(e))
            time.sleep(attempt * attempt)

    raise CyclecloudSlurmError(str(last_exception))


def hostlist(hostlist_expr: str) -> List[str]:
    if hostlist_expr == "*":

        all_node_names = slutil.check_output(
            ["sinfo", "-O", "nodelist", "-h", "-N"]
        ).split()
        return all_node_names
    return slutil.from_hostlist(hostlist_expr)


def hostlist_null_star(hostlist_expr) -> Optional[List[str]]:
    if hostlist_expr == "*":
        return None
    return slutil.from_hostlist(hostlist_expr)


def _as_nodes(node_list: List[str], node_mgr: NodeManager) -> List[Node]:
    nodes: List[Node] = []
    by_name = hpcutil.partition_single(node_mgr.get_nodes(), lambda node: node.name)
    for node_name in node_list:
        # TODO error handling on missing node names
        if node_name not in by_name:
            raise CyclecloudSlurmError(f"Unknown node - {node_name}")
        nodes.append(by_name[node_name])
    return nodes


_IS_AUTOSCALE_ENABLED = None


def is_autoscale_enabled(subprocess_module=None):
    global _IS_AUTOSCALE_ENABLED
    if _IS_AUTOSCALE_ENABLED is not None:
        return _IS_AUTOSCALE_ENABLED
    if subprocess_module is None:
        import subprocess as subprocess_module

    try:
        lines = (
            subprocess_module.check_output(["scontrol", "show", "config"])
            .decode()
            .strip()
            .splitlines()
        )
    except Exception:
        try:
            with open("/sched/slurm.conf") as fr:
                lines = fr.readlines()
        except Exception:
            _IS_AUTOSCALE_ENABLED = True
            return _IS_AUTOSCALE_ENABLED

    for line in lines:
        line = line.strip()
        # this can be defined more than once
        if line.startswith("SuspendTime ") or line.startswith("SuspendTime="):
            suspend_time = line.split("=")[1].strip().split()[0]
            try:
                if suspend_time == "NONE" or int(suspend_time) < 0:
                    _IS_AUTOSCALE_ENABLED = False
                else:
                    _IS_AUTOSCALE_ENABLED = True
            except Exception:
                pass
    return _IS_AUTOSCALE_ENABLED


def main(argv: Optional[Iterable[str]] = None) -> None:
    clilibmain(argv or sys.argv[1:], "slurm", SlurmCLI())


if __name__ == "__main__":
    main()