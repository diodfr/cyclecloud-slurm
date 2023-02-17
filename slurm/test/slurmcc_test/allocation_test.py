from typing import Callable, Dict, List, Tuple

from hpc.autoscale import clock
from hpc.autoscale.ccbindings.mock import MockClusterBinding
from hpc.autoscale.node.node import Node
from slurmcc import allocation
from slurmcc.partition import fetch_partitions

from . import testutil


class MockWaiter(allocation.WaitForResume):
    def __init__(self) -> None:
        super().__init__()

    def check_nodes(
        self, node_list: List[str], latest_nodes: List[Node]
    ) -> Tuple[Dict, List[Node]]:
        return super().check_nodes(node_list, latest_nodes)


def test_basic_resume() -> None:
    node_mgr = testutil.make_test_node_manager()
    bindings: MockClusterBinding = node_mgr.cluster_bindings  # type: ignore
    node_list = ["hpc-1", "hpc-2", "htc-1"]
    partitions = fetch_partitions(node_mgr)
    native_cli = testutil.make_native_cli()

    bootup_result = allocation.resume(testutil.CONFIG, node_mgr, node_list, partitions)
    assert bootup_result
    assert bootup_result.nodes
    assert len(bootup_result.nodes) == 3
    assert node_list == [n.name for n in bootup_result.nodes]

    def get_latest_nodes() -> List[Node]:
        new_node_mgr = testutil.refresh_test_node_manager(node_mgr)
        return new_node_mgr.get_nodes()

    waiter = MockWaiter()
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    assert len(ready) == 0

    bindings.assign_ip(["hpc-2", "htc-1"])
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    assert len(ready) == 0
    assert native_cli.slurm_nodes["hpc-2"]["NodeAddr"] == "hpc-2"
    assert native_cli.slurm_nodes["htc-1"]["NodeAddr"] == "10.1.0.3"

    bindings.update_state("Ready", ["hpc-2"])
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    assert ["hpc-2"] == [n.name for n in ready]

    bindings.update_state("Ready", ["hpc-1", "htc-1"])
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    # hpc-1 should not be ready - it still has no ip address
    assert ["hpc-2", "htc-1"] == [n.name for n in ready]

    bindings.assign_ip(["hpc-1"])
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    # hpc-1 should not be ready - it still has no ip address
    assert node_list == [n.name for n in ready]


def test_mixed_resume_names() -> None:
    node_mgr = testutil.make_test_node_manager()
    node_list = ["hpc-4", "hpc-20"]
    partitions = fetch_partitions(node_mgr)

    bootup_result = allocation.resume(testutil.CONFIG, node_mgr, node_list, partitions)
    assert bootup_result
    assert bootup_result.nodes
    assert len(bootup_result.nodes) == 2
    assert node_list == [n.name for n in bootup_result.nodes]


def test_failure_mode() -> None:
    node_mgr = testutil.make_test_node_manager()
    bindings: MockClusterBinding = node_mgr.cluster_bindings  # type: ignore
    node_list = ["hpc-1", "hpc-2", "htc-1"]
    partitions = fetch_partitions(node_mgr)
    native_cli = testutil.make_native_cli()

    bootup_result = allocation.resume(testutil.CONFIG, node_mgr, node_list, partitions)
    assert bootup_result
    assert bootup_result.nodes
    assert len(bootup_result.nodes) == 3
    assert node_list == [n.name for n in bootup_result.nodes]

    def get_latest_nodes() -> List[Node]:
        new_node_mgr = testutil.refresh_test_node_manager(node_mgr)
        return new_node_mgr.get_nodes()

    waiter = MockWaiter()
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    assert len(ready) == 0

    bindings.assign_ip(node_list)
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())

    assert native_cli.slurm_nodes["htc-1"]["NodeAddr"] == "10.1.0.4"

    # make sure new IPs are assigned - cyclecloud often does this
    bindings.assign_ip(["htc-1"])
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    assert native_cli.slurm_nodes["htc-1"]["NodeAddr"] == "10.1.0.5"

    # make sure we unassign the IP for a failed node
    bindings.update_state("Failed", ["htc-1"])
    states, ready = waiter.check_nodes(node_list, get_latest_nodes())
    assert native_cli.slurm_nodes["htc-1"]["NodeAddr"] == "htc-1", states
