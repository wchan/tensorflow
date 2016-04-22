/* Copyright 2015 Google Inc. All Rights Reserved.

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.
==============================================================================*/

#include "tensorflow/core/kernels/cwise_ops_common.h"

namespace tensorflow {
REGISTER5(UnaryOp, CPU, "Abs", functor::abs, float, Eigen::half, double, int32,
          int64);
#if !defined(__ANDROID__)
REGISTER_KERNEL_BUILDER(Name("ComplexAbs").Device(DEVICE_CPU),
                        UnaryOp<CPUDevice, functor::abs<complex64>>);
#endif
#if GOOGLE_CUDA
REGISTER4(UnaryOp, GPU, "Abs", functor::abs, float, Eigen::half, double, int64);

// A special GPU kernel for int32.
// TODO(b/25387198): Also enable int32 in device memory. This kernel
// registration requires all int32 inputs and outputs to be in host memory.
REGISTER_KERNEL_BUILDER(Name("Abs")
                            .Device(DEVICE_GPU)
                            .HostMemory("x")
                            .HostMemory("y")
                            .TypeConstraint<int32>("T"),
                        UnaryOp<CPUDevice, functor::abs<int32>>);
#endif

}  // namespace tensorflow
