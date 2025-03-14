from typing import Any

import binpacking
import pytest
import torch

from torchsim.autobatching import (
    ChunkingAutoBatcher,
    HotSwappingAutoBatcher,
    calculate_memory_scaler,
    determine_max_batch_size,
)
from torchsim.models.lennard_jones import LennardJonesModel
from torchsim.optimizers import unit_cell_fire
from torchsim.state import BaseState, split_state


def test_calculate_scaling_metric(si_base_state: BaseState) -> None:
    """Test calculation of scaling metrics for a state."""
    # Test n_atoms metric
    n_atoms_metric = calculate_memory_scaler(si_base_state, "n_atoms")
    assert n_atoms_metric == si_base_state.n_atoms

    # Test n_atoms_x_density metric
    density_metric = calculate_memory_scaler(si_base_state, "n_atoms_x_density")
    volume = torch.abs(torch.linalg.det(si_base_state.cell[0])) / 1000
    expected = si_base_state.n_atoms * (si_base_state.n_atoms / volume.item())
    assert pytest.approx(density_metric, rel=1e-5) == expected

    # Test invalid metric
    with pytest.raises(ValueError, match="Invalid metric"):
        calculate_memory_scaler(si_base_state, "invalid_metric")


def test_split_state(si_double_base_state: BaseState) -> None:
    """Test splitting a batched state into individual states."""
    split_states = split_state(si_double_base_state)

    # Check we get the right number of states
    assert len(split_states) == 2

    # Check each state has the correct properties
    for state in enumerate(split_states):
        assert state[1].n_batches == 1
        assert torch.all(
            state[1].batch == 0
        )  # Each split state should have batch indices reset to 0
        assert state[1].n_atoms == si_double_base_state.n_atoms // 2
        assert state[1].positions.shape[0] == si_double_base_state.n_atoms // 2
        assert state[1].cell.shape[0] == 1


def test_chunking_auto_batcher(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    """Test ChunkingAutoBatcher with different states."""
    # Create a list of states with different sizes
    states = [si_base_state, fe_fcc_state]

    # Initialize the batcher with a fixed max_metric to avoid GPU memory testing
    batcher = ChunkingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=260.0,  # Set a small value to force multiple batches
    )
    batcher.load_states(states)

    # Check that the batcher correctly identified the metrics
    assert len(batcher.memory_scalers) == 2
    assert batcher.memory_scalers[0] == si_base_state.n_atoms
    assert batcher.memory_scalers[1] == fe_fcc_state.n_atoms

    # Get batches until None is returned
    batches = list(batcher)

    # Check we got the expected number of batches
    assert len(batches) == len(batcher.batched_states)

    # Test restore_original_order
    restored_states = batcher.restore_original_order(batches)
    assert len(restored_states) == len(states)

    # Check that the restored states match the original states in order
    assert restored_states[0].n_atoms == states[0].n_atoms
    assert restored_states[1].n_atoms == states[1].n_atoms

    # Check atomic numbers to verify the correct order
    assert torch.all(restored_states[0].atomic_numbers == states[0].atomic_numbers)
    assert torch.all(restored_states[1].atomic_numbers == states[1].atomic_numbers)


def test_chunking_auto_batcher_with_indices(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    """Test ChunkingAutoBatcher with return_indices=True."""
    states = [si_base_state, fe_fcc_state]

    batcher = ChunkingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=260.0,
        return_indices=True,
    )
    batcher.load_states(states)

    # Get batches with indices
    batches_with_indices = []
    for batch, indices in batcher:
        batches_with_indices.append((batch, indices))

    # Check we got the expected number of batches
    assert len(batches_with_indices) == len(batcher.batched_states)

    # Check that the indices match the expected bin indices
    for i, (_, indices) in enumerate(batches_with_indices):
        assert indices == batcher.index_bins[i]


def test_chunking_auto_batcher_restore_order_with_split_states(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    """Test ChunkingAutoBatcher's restore_original_order method with split states."""
    # Create a list of states with different sizes
    states = [si_base_state, fe_fcc_state]

    # Initialize the batcher with a fixed max_metric to avoid GPU memory testing
    batcher = ChunkingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=260.0,  # Set a small value to force multiple batches
    )
    batcher.load_states(states)

    # Get batches until None is returned
    batches = []
    while True:
        batch = batcher.next_batch()
        if batch is None:
            break
        # Split each batch into individual states to simulate processing
        # split_batch = split_state(batch)
        batches.append(batch)

    # Test restore_original_order with split states
    # This tests the chain.from_iterable functionality
    restored_states = batcher.restore_original_order(batches)

    # Check we got the right number of states back
    assert len(restored_states) == len(states)

    # Check that the restored states match the original states in order
    assert restored_states[0].n_atoms == states[0].n_atoms
    assert restored_states[1].n_atoms == states[1].n_atoms

    # Check atomic numbers to verify the correct order
    assert torch.all(restored_states[0].atomic_numbers == states[0].atomic_numbers)
    assert torch.all(restored_states[1].atomic_numbers == states[1].atomic_numbers)


def test_hotswapping_max_metric_too_small(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    """Test HotswappingAutoBatcher with different states."""
    # Create a list of states
    states = [si_base_state, fe_fcc_state]

    # Initialize the batcher with a fixed max_metric
    batcher = HotSwappingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=1.0,  # Set a small value to force multiple batches
    )
    # Get the first batch
    with pytest.raises(ValueError, match="is greater than max_metric"):
        batcher.load_states(states)


def test_hotswapping_auto_batcher(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    """Test HotswappingAutoBatcher with different states."""
    # Create a list of states
    states = [si_base_state, fe_fcc_state]

    # Initialize the batcher with a fixed max_metric
    batcher = HotSwappingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=260,  # Set a small value to force multiple batches
        return_indices=True,
    )
    batcher.load_states(states)

    # Get the first batch
    first_batch, [], _ = batcher.next_batch(states, None)
    assert isinstance(first_batch, BaseState)

    # Create a convergence tensor where the first state has converged
    convergence = torch.tensor([True])

    # Get the next batch
    next_batch, popped_batch, idx = batcher.next_batch(first_batch, convergence)
    assert isinstance(next_batch, BaseState)
    assert isinstance(popped_batch, list)
    assert isinstance(popped_batch[0], BaseState)
    assert idx == [1]

    # Check that the converged state was removed
    assert len(batcher.current_scalers) == 1
    assert len(batcher.current_idx) == 1
    assert len(batcher.completed_idx_og_order) == 1

    # Create a convergence tensor where the remaining state has converged
    convergence = torch.tensor([True])

    # Get the next batch, which should be None since all states have converged
    final_batch, popped_batch, _ = batcher.next_batch(next_batch, convergence)
    assert final_batch is None

    # Check that all states are marked as completed
    assert len(batcher.completed_idx_og_order) == 2


def test_determine_max_batch_size_fibonacci(
    si_base_state: BaseState, lj_calculator: LennardJonesModel, monkeypatch: Any
) -> None:
    """Test that determine_max_batch_size uses Fibonacci sequence correctly."""

    # Mock measure_model_memory_forward to avoid actual GPU memory testing
    def mock_measure(*_args: Any, **_kwargs: Any) -> float:
        return 0.1  # Return a small constant memory usage

    monkeypatch.setattr(
        "torchsim.autobatching.measure_model_memory_forward", mock_measure
    )

    # Test with a small max_atoms value to limit the sequence
    max_size = determine_max_batch_size(si_base_state, lj_calculator, max_atoms=10)

    # The Fibonacci sequence up to 10 is [1, 2, 3, 5, 8, 13]
    # Since we're not triggering OOM errors with our mock, it should
    # return the largest value < max_atoms
    assert max_size == 8


def test_hotswapping_auto_batcher_restore_order(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    """Test HotswappingAutoBatcher's restore_original_order method."""
    states = [si_base_state, fe_fcc_state]

    batcher = HotSwappingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=260.0,
    )
    batcher.load_states(states)

    # Get the first batch
    first_batch, [] = batcher.next_batch(states, None)

    # Simulate convergence of all states
    completed_states_list = []
    convergence = torch.tensor([True])
    next_batch, completed_states = batcher.next_batch(first_batch, convergence)
    completed_states_list.extend(completed_states)

    # sample batch a second time
    # sample batch a second time
    next_batch, completed_states = batcher.next_batch(next_batch, convergence)
    completed_states_list.extend(completed_states)

    # Test restore_original_order
    restored_states = batcher.restore_original_order(completed_states_list)
    assert len(restored_states) == 2

    # Check that the restored states match the original states in order
    assert restored_states[0].n_atoms == states[0].n_atoms
    assert restored_states[1].n_atoms == states[1].n_atoms

    # Check atomic numbers to verify the correct order
    assert torch.all(restored_states[0].atomic_numbers == states[0].atomic_numbers)
    assert torch.all(restored_states[1].atomic_numbers == states[1].atomic_numbers)

    # # Test error when number of states doesn't match
    # with pytest.raises(
    #     ValueError, match="Number of completed states .* does not match"
    # ):
    #     batcher.restore_original_order([si_base_state])


def test_hotswapping_with_fire(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    fire_init, fire_update = unit_cell_fire(lj_calculator)

    si_fire_state = fire_init(si_base_state)
    fe_fire_state = fire_init(fe_fcc_state)

    fire_states = [si_fire_state, fe_fire_state] * 5
    fire_states = [state.clone() for state in fire_states]
    for state in fire_states:
        state.positions += torch.randn_like(state.positions) * 0.01

    batcher = HotSwappingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        # max_metric=400_000,
        max_memory_scaler=600,
    )
    batcher.load_states(fire_states)

    def convergence_fn(state: BaseState) -> bool:
        batch_wise_max_force = torch.zeros(
            state.n_batches, device=state.device, dtype=torch.float64
        )
        max_forces = state.forces.norm(dim=1)
        batch_wise_max_force = batch_wise_max_force.scatter_reduce(
            dim=0,
            index=state.batch,
            src=max_forces,
            reduce="amax",
        )
        return batch_wise_max_force < 5e-1

    all_completed_states, convergence_tensor = [], None
    while True:
        print(f"Starting new batch of {state.n_batches} states.")

        state, completed_states = batcher.next_batch(state, convergence_tensor)
        print("Number of completed states", len(completed_states))

        all_completed_states.extend(completed_states)
        if state is None:
            break

        # run 10 steps, arbitrary number
        for _ in range(10):
            state = fire_update(state)
        convergence_tensor = convergence_fn(state)

    assert len(all_completed_states) == len(fire_states)


def test_chunking_auto_batcher_with_fire(
    si_base_state: BaseState, fe_fcc_state: BaseState, lj_calculator: LennardJonesModel
) -> None:
    fire_init, fire_update = unit_cell_fire(lj_calculator)

    si_fire_state = fire_init(si_base_state)
    fe_fire_state = fire_init(fe_fcc_state)

    fire_states = [si_fire_state, fe_fire_state] * 5
    fire_states = [state.clone() for state in fire_states]
    for state in fire_states:
        state.positions += torch.randn_like(state.positions) * 0.01

    batch_lengths = [state.n_atoms for state in fire_states]
    optimal_batches = binpacking.to_constant_volume(batch_lengths, 400)
    optimal_n_batches = len(optimal_batches)

    batcher = ChunkingAutoBatcher(
        model=lj_calculator,
        memory_scales_with="n_atoms",
        max_memory_scaler=400,
    )
    batcher.load_states(fire_states)

    finished_states = []
    n_batches = 0
    for batch in batcher:
        n_batches += 1
        for _ in range(5):
            batch = fire_update(batch)

        finished_states.extend(split_state(batch))

    restored_states = batcher.restore_original_order(finished_states)
    assert len(restored_states) == len(fire_states)
    for restored, original in zip(restored_states, fire_states, strict=False):
        assert torch.all(restored.atomic_numbers == original.atomic_numbers)
    # analytically determined to be optimal
    assert n_batches == optimal_n_batches
