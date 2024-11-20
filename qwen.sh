export CUDA_VISIBLE_DEVICES=3
export FLAGS_enable_pir_api=0
export LD_LIBRARY_PATH=/root/paddlejob/workspace/env_run/output/changwenbin/Research/TensorRT-10.3.0.26/lib/:$LD_LIBRARY_PATH
export LD_LIBRARY_PATH=/root/paddlejob/workspace/env_run/output/changwenbin/Paddle/paddle/phi/kernels/fusion/cutlass/conv2d/build:$LD_LIBRARY_PATH
export LD_LIBRARY_PATH=/root/paddlejob/workspace/env_run/output/changwenbin/Paddle/paddle/phi/kernels/fusion/cutlass/gemm_epilogue/build:$LD_LIBRARY_PATH

export FLAGS_allocator_strategy=auto_growth
# export FLAGS_allocator_strategy=naive_best_fit
# nsys profile -o /root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX/paddlemix/examples/qwen2_vl/Aibin_test/qwen2vl_profile \
python paddlemix/examples/qwen2_vl/single_image_infer.py \
    --benchmark 1
# python paddlemix/examples/qwen2_vl/multi_image_infer.py
# python paddlemix/examples/qwen2_vl/video_infer.py
