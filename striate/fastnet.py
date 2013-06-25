'''
Created on Jun 11, 2013

@author: justin
'''

from pycuda import gpuarray, driver as cuda, autoinit
import numpy as np
import cudaconv2
from pycuda import cumath
from util import *

import sys

def printMatrix(x, name):
  print name
  a = x.get()[:, 0]
  for i in a:
    print '%.15f ' % i

PFout = False
PBout = False
TEST = 0
TRAIN = 1

class Layer(object):

  def __init__(self, name, type):
    self.name = name
    self.type = type

  def fprop(self, input, output):
    assert False, "No implementation for fprop"

  def bprop(self, grad, input, output, outGrad):
    assert False, "No implementation for bprop"

  def update(self):
    pass

  def scaleLearningRate(self, l):
    pass

  def get_output_shape(self):
    assert False, 'No implementation for getoutputshape'

  def change_batch_size(self, batch_size):
    self.batchSize = batch_size

  def dump(self):
    d = {}
    attr = [att for att in dir(self) if not att.startswith('__')]
    for att in attr:
      if type(getattr(self, att)) != type(self.__init__):
        d[att] = getattr(self, att)
    return d


class ConvLayer(Layer):
  def __init__(self , name, filter_shape, image_shape,  padding = 2, stride = 1, initW = 0.01, initB =
      0.0, epsW = 0.001, epsB = 0.002, bias = None, weight = None):
    Layer.__init__(self, name, 'conv')

    self.filterSize = filter_shape[2]
    self.numFilter = filter_shape[0]

    self.batchSize, self.numColor, self.imgSize, _ = image_shape
    self.padding = padding
    self.stride = stride
    self.initW = initW
    self.initB = initB
    self.epsW = epsW
    self.epsB = epsB

    self.outputSize = 1 + int(((2 * self.padding + self.imgSize - self.filterSize) / float(self.stride)))
    self.modules = self.outputSize ** 2

    if weight is None:
      self.filter = gpuarray.to_gpu(np.random.randn(self.filterSize * self.filterSize *
        self.numColor, self.numFilter) * self.initW).astype(np.float32)
    else:
      self.filter = gpuarray.to_gpu(weight).astype(np.float32)

    if bias is None:
      self.bias = gpuarray.to_gpu(np.random.randn(self.numFilter, 1) * initB).astype(np.float32)
    else:
      self.bias = gpuarray.to_gpu(bias).astype(np.float32)

    self.filterGrad = gpuarray.zeros_like(self.filter)
    self.biasGrad = gpuarray.zeros_like(self.bias)

  def dump(self):
    d = Layer.dump(self)
    del d['filterGrad'], d['biasGrad'] , d['tmp']
    d['filter'] = self.filter.get()
    d['bias'] = self.bias.get()
    return d


  def get_single_img_size(self):
    return self.modules * self.numFilter

  def get_output_shape(self):
    self.outputShape = (self.batchSize, self.numFilter, self.outputSize, self.outputSize)
    return self.outputShape


  def fprop(self, input, output):
    cudaconv2.convFilterActs(input, self.filter, output, self.imgSize, self.outputSize,
        self.outputSize, -self.padding, self.stride, self.numColor, 1)

    self.tmp = gpuarray.empty((self.numFilter, self.get_single_img_size() * self.batchSize/self.numFilter), dtype=np.float32)
    gpu_copy_to(output, self.tmp)
    add_vec_to_rows(self.tmp, self.bias)
    gpu_copy_to(self.tmp, output)

    if PFout:
      printMatrix(output, self.name)

  def bprop(self, grad, input, output, outGrad):
    cudaconv2.convImgActs(grad, self.filter, outGrad, self.imgSize, self.imgSize,
        self.outputSize, -self.padding, self.stride, self.numColor, 1, 0.0, 1.0)
    #bprop weight
    self.filterGrad.fill(0)
    cudaconv2.convWeightActs(input, grad, self.filterGrad, self.imgSize, self.outputSize,
        self.outputSize, self.filterSize, -self.padding, self.stride, self.numColor, 1, 0, 1, 1)
    #bprop bias
    self.biasGrad.fill(0)
    gpu_copy_to(grad,self.tmp)
    add_row_sum_to_vec(self.biasGrad, self.tmp)

    if PBout:
      printMatrix(outGrad, self.name)

  def update(self):
    self.filter = self.filter.mul_add(1, self.filterGrad, self.epsW / self.batchSize)
    self.bias = self.bias.mul_add(1, self.biasGrad, self.epsB /self.batchSize)

  def scaleLearningRate(self, lr):
    self.epsW *= lr
    self.epsB *= lr

class MaxPoolLayer(Layer):
  def __init__(self,  name, image_shape,  poolSize = 2, stride = 2, start = 0):
    Layer.__init__(self, name, 'pool')
    self.poolSize = poolSize
    self.stride = stride
    self.start = start

    self.batchSize, self.numColor, self.imgSize, _  = image_shape

    self.outputSize = ceil(self.imgSize - self.poolSize -self.start, self.stride) + 1

  def get_output_shape(self):
    self.outputShape = (self.batchSize, self.numColor, self.outputSize, self.outputSize)
    return self.outputShape

  def fprop(self, input, output):
    cudaconv2.convLocalMaxPool(input, output, self.numColor, self.poolSize, self.start, self.stride,
        self.outputSize)

    if PFout:
      printMatrix(output, self.name)

  def bprop(self, grad, input, output, outGrad):
    cudaconv2.convLocalMaxUndo(input, grad, output, outGrad, self.poolSize,
        self.start, self.stride, self.outputSize, 0.0, 1.0)

    if PBout:
      printMatrix(outGrad, self.name)

class ResponseNormLayer(Layer):
  def __init__(self, name, image_shape, pow, size, scale):
    Layer.__init__(self, name, 'rnorm')
    self.batchSize,self.numColor, self.imgSize, _ = image_shape

    self.pow = pow
    self.size = size
    self.scale = scale
    self.denom = None

  def get_output_shape(self):
    self.outputShape = (self.batchSize, self.numColor, self.imgSize, self.imgSize)
    return self.outputShape

  def fprop(self, input, output):
    self.denom = gpuarray.zeros_like(input)
    cudaconv2.convResponseNorm(input, self.denom, output, self.numColor, self.size, self.scale,
        self.pow)

    if PFout:
      printMatrix(output, self.name)


  def bprop(self, grad,input, output, outGrad):
    cudaconv2.convResponseNormUndo(grad, self.denom, input, output, outGrad, self.numColor,
        self.size, self.scale, self.pow, 0.0, 1.0)

    if PBout:
      printMatrix(outGrad, self.name)

  def dump(self):
    d = Layer.dump(self)
    del d['denom']
    return d

class FCLayer(Layer):
  def __init__(self, name, input_shape, n_out, epsW=0.001, epsB=0.002, initW = 0.01, initB = 0.0, weight =
      None, bias = None):
    Layer.__init__(self, name, 'fc')
    self.epsW = epsW
    self.epsB = epsB
    self.initW = initW
    self.initB = initB

    self.inputSize, self.batchSize = input_shape
    self.outputSize = n_out

    self.weightShape = (self.outputSize, self.inputSize)
    if weight is None:
      self.weight = gpuarray.to_gpu(np.random.randn(*self.weightShape) *
          self.initW).astype(np.float32)
    else:
      self.weight = gpuarray.to_gpu(weight).astype(np.float32)
      self.weight = transpose(self.weight)

    if bias is None:
      self.bias = gpuarray.to_gpu(np.random.randn(self.outputSize, 1) *
          self.initB).astype(np.float32)
    else:
      self.bias = gpuarray.to_gpu(bias).astype(np.float32)
    self.weightGrad = gpuarray.zeros_like(self.weight)
    self.biasGrad = gpuarray.zeros_like(self.bias)


  def dump(self):
    d = Layer.dump(self)
    del d['weightGrad'], d['biasGrad']
    d['weight'] = self.weight.get()
    d['bias'] = self.bias.get()
    return d

  def get_output_shape(self):
    self.outputShape = (self.batchSize, self.outputSize, 1, 1)
    return self.outputShape

  def fprop(self, input, output ):
    gpu_copy_to( output.mul_add(0, dot(self.weight, input), 1), output)
    add_vec_to_rows(output, self.bias)

    if PFout:
      printMatrix(output, self.name)

  def bprop(self, grad, input, output, outGrad):
    gpu_copy_to(outGrad.mul_add(0, dot(transpose(self.weight), grad), 1), outGrad)
    self.weightGrad = self.weightGrad.mul_add(0, dot(grad, transpose(input)), 1)
    add_row_sum_to_vec(self.biasGrad, grad, alpha = 0.0)

    if PBout:
      printMatrix(outGrad, self.name)

  def update(self):
    self.weight = self.weight.mul_add(1, self.weightGrad, self.epsW / self.batchSize)
    self.bias = self.bias.mul_add(1, self.biasGrad, self.epsB / self.batchSize)

  def scaleLearningRate(self, l):
    self.epsW *= l
    self.epsB *= l


class SoftmaxLayer(Layer):
  def __init__(self, name, input_shape):
    Layer.__init__(self, name, "softmax")
    self.inputSize, self.batchSize = input_shape
    self.outputSize = self.inputSize
    self.cost = gpuarray.zeros((self.batchSize, 1), dtype = np.float32)
    self.batchCorrect = 0

  def get_output_shape(self):
    self.outputShape = (self.batchSize, self.outputSize, 1, 1)
    return self.outputShape

  def fprop(self, input, output):
    max = gpuarray.zeros((1, self.batchSize), dtype = np.float32)
    col_max_reduce(max, input)
    add_vec_to_cols(input, max, output, alpha = -1)
    gpu_copy_to(cumath.exp(output), output)
    sum = gpuarray.zeros(max.shape, dtype = np.float32)
    add_col_sum_to_vec(sum, output, alpha = 0)
    div_vec_to_cols(output, sum)

    if PFout:
      printMatrix(output, self.name)

  def logreg_cost(self, label, output):
    maxid = gpuarray.zeros((self.batchSize, 1), dtype = np.float32)
    find_col_max_id(maxid, output)
    self.batchCorrect = same_reduce(label , maxid)

    logreg_cost_col_reduce(output, label, self.cost)

  def bprop(self, label, input, output, outGrad):
    softmax_bprop(output, label, outGrad)

    if PBout:
      printMatrix(outGrad, self.name)

  def get_correct(self):
    return  1.0 * self.batchCorrect / self.batchSize

  def dump(self):
    d = Layer.dump(self)
    del d['cost']
    return d


class Neuron:
  def __init__(self): pass

  def activate(self, input, output):
    assert('No Implementation of Activation')

  def computeGrad(self, grad, output, inputGrad):
    assert('No Implementation of Gradient')

class ReluNeuron(Neuron):
  def __init__(self):
    Neuron.__init__(self)

  def activate(self, input, output):
    relu_activate(input, output)

  def computeGrad(self, grad, output, outGrad):
    relu_compute_grad(grad, output, outGrad)


neuronDict = {'relu': lambda : ReluNeuron(), }


class NeuronLayer(Layer):
  def __init__(self, name, image_shape,  type = 'relu'):
    Layer.__init__(self, name, type)
    self.neuron = neuronDict[type]()
    self.batchSize, self.numColor, self.imgSize, _= image_shape


  def get_output_shape(self):
    self.outputShape = (self.batchSize, self.numColor, self.imgSize, self.imgSize)
    return self.outputShape

  def fprop(self, input, output):
    self.neuron.activate(input, output)

    if PFout:
      printMatrix(output, self.name)

  def bprop(self, grad, input, output, outGrad):
    self.neuron.computeGrad(grad, output, outGrad)
    if PBout:
      printMatrix(outGrad, self.name)

  def dump(self):
    d = Layer.dump(self)
    del d['neuron']
    return d

class FastNet(object):
  def __init__(self, learningRate, imgShape, numOutput, initModel = None, autoAdd = True):
    self.learningRate = learningRate
    self.batchSize, self.numColor, self.imgSize, _ = imgShape
    self.imgShapes = [imgShape]
    self.inputShapes = [( self.numColor * (self.imgSize ** 2), self.batchSize)]
    self.numOutput = numOutput
    self.layers = []
    self.outputs = []
    self.grads = []

    self.numCase = self.cost = self.correct = 0.0

    if initModel:
      self.initLayer(initModel)
      return

    if autoAdd:
      self.autoAddLayer(numOutput)

  def makeLayerFromCUDACONVNET(self, ld):
    if ld['type'] == 'conv':
      numFilter = ld['filters']
      filterSize = ld['filterSize'][0]
      numColor = ld['channels'][0]
      padding = -ld['padding'][0]
      stride = ld['stride'][0]
      initW = ld['initW'][0]
      initB = ld['initB']
      name = ld['name']
      epsW = ld['epsW'][0]
      epsB = ld['epsB']

      imgSize = ld['imgSize']

      bias = ld['biases']
      weight = ld['weights'][0]

      filter_shape = (numFilter, numColor, filterSize, filterSize)
      img_shape = self.imgShapes[-1]
      return ConvLayer(name, filter_shape, img_shape, padding, stride, initW, initB, epsW, epsB, bias,
          weight)

    if ld['type'] == 'pool':
      stride = ld['stride']
      start = ld['start']
      poolSize = ld['sizeX']
      img_shape = self.imgShapes[-1]
      name = ld['name']
      return MaxPoolLayer(name, img_shape, poolSize, stride, start)

    if ld['type'] == 'neuron':
      if ld['neuron']['type'] == 'relu':
        img_shape = self.imgShapes[-1]
        name = ld['name']
        return NeuronLayer(name, img_shape, type = 'relu')

    if ld['type'] == 'fc':
      epsB = ld['epsB']
      epsW = ld['epsW'][0]
      initB = ld['initB']
      initW = ld['initW'][0]

      n_out = ld['outputs']
      bias = ld['biases']
      weight = ld['weights'][0]
      name = ld['name']
      input_shape = self.inputShapes[-1]
      return FCLayer(name, input_shape, n_out, epsW, epsB, initW, initB, weight, bias)

    if ld['type'] == 'softmax':
      name = ld['name']
      input_shape = self.inputShapes[-1]
      return SoftmaxLayer(name, input_shape)

    if ld['type'] == 'rnorm':
      name = ld['name']
      pow = ld['pow']
      size = ld['size']
      scale = ld['scale']

      img_shape = self.imgShapes[-1]
      return ResponseNormLayer(name, img_shape, pow, size, scale)


  def initLayer(self, m):
    layers = m['model_state']['layers']
    for l in layers:
      layer = self.makeLayerFromCUDACONVNET(l)
      if layer:
        layer.scaleLearningRate(self.learningRate)
        self.append_layer(layer)

  def autoAddLayer(self, n_out):
    conv1 = ConvLayer('conv1', filter_shape = (64, 3, 5, 5), image_shape = self.imgShapes[-1],
        padding = 2, stride = 1, initW = 0.0001, epsW = 0.001, epsB = 0.002)
    conv1.scaleLearningRate(self.learningRate)
    self.append_layer(conv1)

    conv1_relu = NeuronLayer('conv1_neuron', self.imgShapes[-1])
    self.append_layer(conv1_relu)

    pool1 = MaxPoolLayer('pool1', self.imgShapes[-1], poolSize = 3, stride = 2, start = 0)
    self.append_layer(pool1)

    rnorm1 = ResponseNormLayer('rnorm1', self.imgShapes[-1], pow = 0.75, scale = 0.001, size = 9)
    self.append_layer(rnorm1)

    conv2 = ConvLayer('conv2',filter_shape = (64, 64, 5, 5) , image_shape = self.imgShapes[-1],
        padding = 2, stride = 1, initW=0.01, epsW = 0.001, epsB = 0.002)
    conv2.scaleLearningRate(self.learningRate)
    self.append_layer(conv2)

    conv2_relu = NeuronLayer('conv2_neuron', self.imgShapes[-1])
    self.append_layer(conv2_relu)

    rnorm2 = ResponseNormLayer('rnorm2', self.imgShapes[-1], pow = 0.75, scale = 0.001, size = 9)
    self.append_layer(rnorm2)

    pool2 = MaxPoolLayer('pool2', self.imgShapes[-1], poolSize= 3, start = 0, stride = 2)
    self.append_layer(pool2)

    fc1 = FCLayer('fc', self.inputShapes[-1], n_out)
    fc1.scaleLearningRate(self.learningRate)
    self.append_layer(fc1)

    softmax1 = SoftmaxLayer('softmax', self.inputShapes[-1])
    self.append_layer(softmax1)


  def append_layer(self, layer):
    self.layers.append(layer)

    outputShape = layer.get_output_shape()
    row = outputShape[1] * outputShape[2] * outputShape[3]
    col = outputShape[0]
    self.inputShapes.append((row, col))
    self.imgShapes.append(outputShape)

    self.outputs.append(gpuarray.zeros((row, col), dtype = np.float32))
    self.grads.append(gpuarray.zeros(self.inputShapes[-2], dtype = np.float32))


  def fprop(self, data, probs):
    input = data
    for i in range(len(self.layers)):
      l = self.layers[i]
      l.fprop(input, self.outputs[i])
      input = self.outputs[i]

    probs.shape = self.outputs[-1].shape
    gpu_copy_to(self.outputs[-1], probs)

  def bprop(self, data, label, prob):
    grad = label
    for i in range(1, len(self.layers) + 1):

      l = self.layers[-i]
      if i == len(self.layers):
        input = data
      else:
        input = self.outputs[-(i+1)]
      output = self.outputs[-i]
      outGrad = self.grads[-i]
      l.bprop(grad, input, output, outGrad)
      grad = outGrad

  def update(self):
    for l in self.layers:
      l.update()

  def get_cost(self, label, output):
    outputLayer = self.layers[-1]
    outputLayer.logreg_cost(label, output)
    return outputLayer.cost.get().sum(), outputLayer.batchCorrect

  def get_batch_information(self):
    cost = self.cost
    numCase = self.numCase
    correct = self.correct
    self.cost = self.numCase = self.correct = 0.0
    return cost/numCase , correct/ numCase, int(numCase)

  def get_correct(self):
    outputLayer = self.layers[-1]
    return outputLayer.get_correct()

  def train_batch(self, data, label, train = TRAIN):
    input = data
    self.numCase += input.shape[1]
    ########
    # The last minibatch of data_batch file may not be 1024
    ########
    if input.shape[1] != self.batchSize:
      self.batchSize = input.shape[1]
      for l in self.layers:
        l.change_batch_size(self.batchSize)
      self.inputShapes = None
      self.imgShapes = None
      self.outputs = []
      self.grads= []

      self.imgShapes = [(self.batchSize, self.numColor, self.imgSize, self.imgSize)]
      self.inputShapes = [( self.numColor * (self.imgSize ** 2), self.batchSize)]
      for layer in self.layers:
        outputShape = layer.get_output_shape()
        row = outputShape[1] * outputShape[2] * outputShape[3]
        col = outputShape[0]
        self.inputShapes.append((row, col))
        self.imgShapes.append(outputShape)

        self.outputs.append(gpuarray.zeros((row, col),dtype=np.float32))
        self.grads.append(gpuarray.zeros(self.inputShapes[-2], dtype=np.float32))

    outputShape = self.inputShapes[-1]
    output = gpuarray.zeros(outputShape, dtype=np.float32)

    if not isinstance(data, GPUArray):
      assert(isinstance(data, np.ndarray))
      data = gpuarray.to_gpu(data.astype(np.float32)) #.astype(np.float32))

    if not isinstance(label, GPUArray):
      assert(isinstance(label, np.ndarray))
      label = gpuarray.to_gpu(label.astype(np.float32))

    self.fprop(data, output)
    cost, correct = self.get_cost(label, output)
    self.cost += cost
    self.correct += correct
    if train == TRAIN:
      self.bprop(data, label, output)
      self.update()

  def get_dumped_layers(self):
    layers = []
    for l in self.layers:
      layers.append(l.dump() )

    return layers
