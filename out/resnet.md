### ResNet

## 一句话总结
ResNet通过引入残差学习框架，使训练深度显著增加的网络更容易优化。

## 研究问题
如何使训练深度显著增加的网络更容易优化？

## 方法要点
1. 将网络层重新定义为学习残差函数，而不是学习未参考的函数。
2. 通过让这些层近似残差函数 \( F(x) := H(x) - x \) 来实现这一点，其中 \( H(x) \) 是底层映射，\( x \) 是输入。

## 关键数据
- 使用ImageNet 2012分类数据集进行实验。

## 创新点
1. 提出了一种残差学习框架，使训练深度显著增加的网络更容易优化。
2. 通过让网络层近似残差函数，而不是学习未参考的函数，解决了深度网络训练中的退化问题。

## 局限与疑问
- 实验结果仅在ImageNet和CIFAR-10数据集上进行，可能不适用于其他数据集。

## 可借鉴点
- 残差学习框架在解决深度网络训练退化问题上具有潜力，值得在其他视觉识别任务中进一步探索。

## 原文依据

- **research_question**：Deeper neural networks are more difficult to train.
- **gap**：The deeper network has higher training error, and thus test error.
- **method**：Let us consider H ( x ) as an underlying mapping to be fit by a few stacked layers (not necessarily the entire net), with x denoting the inputs to the first of these layers.
- **dataset**：The models are trained on the 1.28 million training images, and evaluated on the 50k validation images.
- **metrics**：We evaluate both top-1 and top-5 error rates.
- **key_results**：This result won the 1st place on the ILSVRC 2015 classification task.
- **novelty**：We present a residual learning framework to ease the training of networks that are substantially deeper than those used previously.
- **limitations**：The deeper network has higher training error, and thus test error.
