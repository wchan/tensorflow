#include "tensorflow/core/example/example.pb.h"
#include "tensorflow/core/framework/op.h"
#include "tensorflow/core/framework/op_kernel.h"
#include "tensorflow/core/platform/protobuf.h"

#include "tensorflow/core/platform/logging.h"

using namespace tensorflow;

namespace {
REGISTER_OP("S4ParseUtterance")
    .Attr("features_len_max: int")
    .Attr("tokens_len_max: int")
    .Input("serialized: string")
    .Output("features: features_len_max * float")
    .Output("features_len: int64")
    .Output("text: string")
    .Output("tokens: tokens_len_max * int64")
    .Output("tokens_len: int64")
    .Output("tokens_weights: tokens_len_max * float")
    .Output("uttid: string")
    .Doc(R"doc(
SPEECH4, parse an utterance!
)doc");

class S4ParseUtterance : public OpKernel {
 public:
  explicit S4ParseUtterance(OpKernelConstruction* ctx) : OpKernel(ctx) {
    OP_REQUIRES_OK(ctx, ctx->GetAttr("features_len_max", &features_len_max_));
    OP_REQUIRES_OK(ctx, ctx->GetAttr("tokens_len_max", &tokens_len_max_));
  }

  void Compute(OpKernelContext* ctx) override {
    const Tensor* serialized;
    OP_REQUIRES_OK(ctx, ctx->input("serialized", &serialized));
    auto serialized_t = serialized->vec<string>();
    const int64 batch_size = serialized_t.size();

    OpOutputList output_list_features;
    Tensor* output_tensor_features_len = nullptr;
    Tensor* output_tensor_text = nullptr;
    OpOutputList output_list_tokens;
    Tensor* output_tensor_tokens_len = nullptr;
    Tensor* output_tensor_uttid = nullptr;
    OpOutputList output_list_tokens_weights;

    for (int64 b = 0; b < batch_size; ++b) {
      // Parse our serialized string into our Example proto.
      Example ex;
      OP_REQUIRES(
          ctx, ParseProtoUnlimited(&ex, serialized_t(b)),
          errors::InvalidArgument("Could not parse example input, value: '",
                                  serialized_t(b), "'"));
      const auto& feature_dict = ex.features().feature();

      // Extract the features_len.
      const auto& features_len_iter = feature_dict.find("features_len");
      CHECK(features_len_iter != feature_dict.end());
      const int64 features_len = features_len_iter->second.int64_list().value(0);

      // Extract the features.
      const auto& features_iter = feature_dict.find("features");
      CHECK(features_iter != feature_dict.end());
      const auto& features = features_iter->second.float_list();

      // The features_width is a function of len(features) and features_len.
      CHECK_EQ(features.value().size() % features_len, 0);
      const int64 features_width = features.value().size() / features_len;

      if (b == 0) {
        // Allocate the memory.
        OP_REQUIRES_OK(ctx, ctx->output_list("features", &output_list_features));
        for (int64 t = 0; t < features_len_max_; ++t) {
          TensorShape feature_shape({batch_size, features_width});

          Tensor* feature_slice = nullptr;
          output_list_features.allocate(t, feature_shape, &feature_slice);

          std::fill_n(feature_slice->flat<float>().data(), feature_shape.num_elements(), 0.0f);
        }

        TensorShape x({batch_size});
        OP_REQUIRES_OK(
            ctx, ctx->allocate_output("features_len", TensorShape({batch_size}), &output_tensor_features_len));

        OP_REQUIRES_OK(
            ctx, ctx->allocate_output("text", TensorShape({batch_size}), &output_tensor_text));

        OP_REQUIRES_OK(ctx, ctx->output_list("tokens", &output_list_tokens));

        for (int64 s = 0; s < tokens_len_max_; ++s) {
          TensorShape feature_shape({batch_size});

          Tensor* feature_slice = nullptr;
          output_list_tokens.allocate(s, feature_shape, &feature_slice);

          std::fill_n(feature_slice->flat<int64>().data(), feature_shape.num_elements(), -1);
        }

        OP_REQUIRES_OK(
            ctx, ctx->allocate_output("tokens_len", TensorShape({batch_size}), &output_tensor_tokens_len));

        OP_REQUIRES_OK(ctx, ctx->output_list("tokens", &output_list_tokens_weights));

        for (int64 s = 0; s < tokens_len_max_; ++s) {
          TensorShape feature_shape({batch_size});

          Tensor* feature_slice = nullptr;
          output_list_tokens_weights.allocate(s, feature_shape, &feature_slice);

          std::fill_n(feature_slice->flat<float>().data(), feature_shape.num_elements(), 0.0f);
        }

        OP_REQUIRES_OK(
            ctx, ctx->allocate_output("uttid", TensorShape({batch_size}), &output_tensor_uttid));
      }

      // Copy the features across.
      output_tensor_features_len->flat<int64>().data()[b] = features_len;
      CHECK_LE(features_len, features_len_max_);
      for (int64 t = 0; t < features_len; ++t) {
        Tensor* feature_slice = output_list_features[t];
        std::copy_n(features.value().data() + t * features_width,
                    features_width,
                    feature_slice->flat<float>().data() + b * features_width);
      }

      // Copy the text across.
      const auto& text_iter = feature_dict.find("text");
      CHECK(text_iter != feature_dict.end());
      const auto& text = text_iter->second.bytes_list().value(0);

      output_tensor_text->flat<string>().data()[b] = text;
      
      // Copy the tokens.
      const auto& tokens_iter = feature_dict.find("tokens");
      CHECK(tokens_iter != feature_dict.end());
      const auto& tokens = tokens_iter->second.int64_list();
      const int64 tokens_len = tokens.value().size();

      output_tensor_tokens_len->flat<int64>().data()[b] = tokens_len;
      CHECK_LE(tokens_len, tokens_len_max_);
      for (int64 s = 0; s < tokens_len; ++s) {
        Tensor* token_slice = output_list_tokens[s];
        token_slice->flat<int64>().data()[b] = tokens_len;

        Tensor* weight_slice = output_list_tokens_weights[s];
        weight_slice->flat<float>().data()[b] = 1.0f;
      }

      // Copy the uttid across.
      const auto& uttid_iter = feature_dict.find("uttid");
      CHECK(uttid_iter != feature_dict.end());
      const auto& uttid = uttid_iter->second.bytes_list().value(0);

      output_tensor_uttid->flat<string>().data()[b] = uttid;
    }
  }

 protected:
  int64 features_len_max_;
  int64 tokens_len_max_;
};

REGISTER_KERNEL_BUILDER(Name("S4ParseUtterance").Device(DEVICE_CPU), S4ParseUtterance);
}
