# #!/bin/bash
# export FLAGS_enable_pir_api=0
# export CUDA_VISIBLE_DEVICES=0
# export PYTHONPATH=/root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX/PaddleNLP:/root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX

# python deploy/qwen_vl/export_image_encoder.py \
#     --model_name_or_path "qwen-vl/qwen-vl-7b-static" \
#     --save_path "./checkpoints/encode_image/vision"

# # ./checkpoints/encode_image/vision


#!/bin/bash
export FLAGS_enable_pir_api=1
export CUDA_VISIBLE_DEVICES=0
export PYTHONPATH=/root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX/PaddleNLP:/root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX

python deploy/qwen_vl/run_static_predict.py \
    --first_model_path "/root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX/checkpoints/encode_image/vision" \
    --second_model_path "/root/paddlejob/workspace/env_run/output/changwenbin/PaddleMIX/PaddleNLP/llm/checkpoints/encode_text/qwen" \
    --model_name_or_path "qwen-vl/qwen-vl-7b-static" \
    