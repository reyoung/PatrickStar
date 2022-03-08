# Copyright (C) 2021 THL A29 Limited, a Tencent company.
# All rights reserved.
# Licensed under the BSD 3-Clause License (the "License"); you may
# not use this file except in compliance with the License. You may
# obtain a copy of the License at
# https://opensource.org/licenses/BSD-3-Clause
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" basis,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or
# implied. See the License for the specific language governing
# permissions and limitations under the License.
# See the AUTHORS file for names of contributors.

import torch

from patrickstar.core.comm import CommInfo
from patrickstar.core.const import ChunkState
from patrickstar.core.memtracer import RuntimeMemTracer
from patrickstar.core.parameter import TensorInfo
from patrickstar.utils import get_rank, getsizeof, global_timer


class Chunk:
    def __init__(
        self,
        dtype,
        capacity: int,
        chunk_id: int,
        memory_tracer: RuntimeMemTracer,
        local_rank: int = 0,
    ):
        r"""
        Chunk is the minimal unit of the data transfer.
        It is a contiguous memory for saving tensors.

        Chunk does no know if we are doing distributed training or not.
        Every process will observe its own chunk instances.

        Args:
            capacity: int. The maximum number of elements in the chunk.
            chunk_id: int.
            local_rank: int.
        """
        self.chunk_id = chunk_id
        self.comm_info = CommInfo(chunk_id=chunk_id)
        # payload numel does not equal to capacity. payload can be None.
        self.dtype = dtype
        self.capacity = capacity
        self.local_rank = local_rank
        self.memory_tracer = memory_tracer

        self.payload = None
        self._pin_flag = False

        self.end_pos = 0
        self.params = []
        # the number of params in compute state
        self.num_in_compute = 0

    def is_local(self):
        return get_rank() == self.comm_info.offset

    def get_chunk_space(self):
        r"""Size of the chunk (Bytes)."""
        return getsizeof(torch.half) * self.capacity

    def get_payload_space(self):
        r"""Size of the payload (Bytes)."""
        if self.payload is None:
            return 0
        else:
            return getsizeof(self.payload.dtype) * self.payload.numel()

    def get_device(self):
        r"""Get device of the payload of chunk, return None if not allocated."""
        if self.payload is not None:
            return self.payload.device
        else:
            return None

    def get_state(self):
        r"""
        When payload is None, the state is `RELEASED`,
        otherwise, state of the chunk is decided by its tensors.
        """
        if self.payload is None:
            return ChunkState.RELEASED

        # Distributed training need to fix the chunk on the compute device.
        if self.num_in_compute > 0:
            return ChunkState.COMPUTE
        else:
            return ChunkState.HOLD

    def pin(self):
        self._pin_flag = True

    def unpin(self):
        self._pin_flag = False

    def is_pin(self):
        return self._pin_flag

    def can_fit(self, numel):
        return self.capacity - self.end_pos >= numel

    def add_param(self, param):
        assert param.dtype == torch.float
        numel = param.ps_attr.numel
        if not self.can_fit(numel):
            return False
        self.params.append(param)
        param.ps_attr.info = TensorInfo(self.chunk_id, param, self.end_pos)
        self.end_pos += numel
        return True

    def allocate_payload(self, device):
        r"""Allocate payload on device for the chunk."""
        self.payload = torch.zeros(
            self.capacity,
            dtype=self.dtype,
            device=device,
            pin_memory=(device.type == "cpu"),
        )
        self.memory_tracer.add(
            device.type,
            self.get_payload_space(),
            self.payload.is_pinned(),
        )
        self.state = ChunkState.HOLD

    def release_payload(self):
        r"""Release the payload."""
        self.memory_tracer.delete(
            self.get_device().type,
            self.get_payload_space(),
            self.payload.is_pinned(),
        )
        del self.payload
        self.payload = None
        self.state = ChunkState.RELEASED

    def move(self, target_device: torch.device):
        r"""
        Move the chunk to `target_device` synchronizely.
        NOTE() Please check if the `target_device` has enough room before.

        Args:
            target_device: :class:`torch.device`.
        """
        src_device = self.get_device()
        assert src_device is not None and src_device != target_device

        global_timer.start_profile("move chunk")

        if target_device.type == "cpu":
            tmp = self.payload
            self.payload = torch.empty(
                self.payload.shape,
                dtype=self.payload.dtype,
                device="cpu:0",
                pin_memory=True,
            )
            self.payload.copy_(tmp)
            del tmp
        elif target_device.type == "cuda":
            self.payload = self.payload.pin_memory()
            self.payload = self.payload.to(target_device)

        global_timer.finish_profile("move chunk")

        self.memory_tracer.delete(
            src_device.type,
            self.get_payload_space(),
            self.payload.is_pinned(),
        )
        self.memory_tracer.add(
            target_device.type,
            self.get_payload_space(),
            self.payload.is_pinned(),
        )
