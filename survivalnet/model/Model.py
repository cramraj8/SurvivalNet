
import numpy

import theano
import theano.tensor as T
from theano.tensor.shared_randomstreams import RandomStreams

from .RiskLayer import RiskLayer
from .HiddenLayer import HiddenLayer
from .DropoutHiddenLayer import DropoutHiddenLayer
from .SparseDenoisingAutoencoder import SparseDenoisingAutoencoder as dA
from survivalnet.optimization import Optimization as Opt
from theano.compile.nanguardmode import NanGuardMode


class Model(object):
    """ This class is made to pretrain and fine tune a variable number of layers."""
    def __init__(
        self,
        numpy_rng,
        theano_rng=None,
        n_ins=183,
        hidden_layers_sizes=[250, 250],
        n_outs=1,
        corruption_levels=[0.1, 0.1],
        dropout_rate=0.1,
    	lambda1 = 0,
    	lambda2 = 0,
        non_lin=None
    ):
        """        
        :type numpy_rng: numpy.random.RandomState
        :param numpy_rng: numpy random number generator used to draw initial
                    weights
        :type theano_rng: theano.tensor.shared_randomstreams.RandomStreams
        :param theano_rng: Theano random generator; if None is given one is
                           generated based on a seed drawn from `rng`
        :type n_ins: int
        :param n_ins: dimension of the input to the Model
       
        :type hidden_layers_sizes: list of ints
        :param hidden_layers_sizes: sizes of intermediate layers. 
 
        :type n_outs: int
        :param n_outs:  dimension of the output of the network. Always 1 for a 
                        regression problem.
 
        :type corruption_levels: list of float
        :param corruption_levels: amount of corruption to use for each layer
        
        :type dropout_rate: float
        :param dropout_rate: probability of dropping a hidden unit
        
        :type non_lin: function
        :param non_lin: nonlinear activation function used in all layers
                
        """
        # Initialize parameters
        self.hidden_layers = []; self.dA_layers = []; self.params = []; self.dropout_masks = []
        self.n_layers = len(hidden_layers_sizes); self.L1 = 0; self.L2_sqr = 0; self.n_hidden = hidden_layers_sizes[0]
        if not theano_rng:
            theano_rng = RandomStreams(numpy_rng.randint(2 ** 30))

        # Allocate symbolic variables for the data
        self.x = T.matrix('x', dtype='float32')                                                 # Expression data
        self.o = T.ivector('o')                                                                 # Observed death or not, 1 is death, 0 is right censored
        self.AtRisk = T.ivector('AtRisk')        
        self.is_train = T.iscalar('is_train')                                                   # Indicator of training/testing used in dropout
        self.masks = [T.lmatrix('mask_' + str(i)) for i in range(self.n_layers)]

        # Simple cox regression with no hidden layers
        if self.n_layers == 0:                              
            self.riskLayer = RiskLayer(input=self.x, n_in=n_ins, n_out=n_outs, rng = numpy_rng)
        else:    
            # Construct the intermediate layer
            # the size of the input is either the number of hidden units of
            # the layer below or the input size if we are on the first layer
            for i in xrange(self.n_layers):     
                if i == 0:
                    input_size = n_ins
                    layer_input = self.x
                else:
                    input_size = hidden_layers_sizes[i - 1]
                    layer_input = self.hidden_layers[-1].output

                # The input to this layer is either the activation of the hidden
                # layer below or the input of the Model if you are on the first layer
                hidden_layer = DropoutHiddenLayer(rng=numpy_rng,
                                            input=layer_input,
                                            n_in=input_size,
                                            n_out=hidden_layers_sizes[i],
                                            activation=non_lin,
                                            dropout_rate=dropout_rate,
                                            is_train=self.is_train,
                                            mask = self.masks[i]) \
                    if dropout_rate > 0 else HiddenLayer(rng=numpy_rng,
                                            input=layer_input,
                                            n_in=input_size,
                                            n_out=hidden_layers_sizes[i],
                                            activation=non_lin)

                # Add the layer to our stack of layers
                self.hidden_layers.append(hidden_layer)
                self.params.extend(hidden_layer.params)
    
                # Construct a denoising autoencoder that shares weights with this layer
                dA_layer = dA(numpy_rng=numpy_rng,
                              theano_rng=theano_rng,
                              input=layer_input,
                              n_visible=input_size,
                              n_hidden=hidden_layers_sizes[i],
                              W=hidden_layer.W,
                              bhid=hidden_layer.b,
                              non_lin=non_lin)
                self.dA_layers.append(dA_layer)

        self.L1 += abs(hidden_layer.W).sum()
        self.L2_sqr += (hidden_layer.W ** 2).sum()

        # We now need to add a risk prediction layer on top of the stack
        self.riskLayer = RiskLayer(
            input=self.hidden_layers[-1].output,
            n_in=hidden_layers_sizes[-1],
            n_out=n_outs,
            rng = numpy_rng)

        self.L1 += abs(self.riskLayer.W).sum()
        self.L2_sqr += (self.riskLayer.W ** 2).sum()
        self.L1 *= lambda1
        self.L2_sqr *= lambda2
        self.params.extend(self.riskLayer.params)
        #cost = self.riskLayer.cost + self.L1 + self.L2_sqr
        
    def pretraining_functions(self, pretrain_x, batch_size):
        index = T.lscalar('index')                  # index to a minibatch
        corruption_level = T.scalar('corruption')   # % of corruption
        learning_rate = T.scalar('lr')              # learning rate


        if batch_size:
            # begining of a batch, given `index`
            batch_begin = index * batch_size
            # ending of a batch given `index`
            batch_end = batch_begin + batch_size
            pretrain_x = pretrain_x[batch_begin: batch_end]

        pretrain_fns = []
        is_train = numpy.cast['int32'](0)   # value does not matter
        for dA_layer in self.dA_layers:
            # get the cost and the updates list
            cost, updates = dA_layer.get_cost_updates(corruption_level,
                                                learning_rate)
            # compile the theano function
            fn = theano.function(
                on_unused_input='ignore',
                inputs=[
                    index,
                    theano.Param(corruption_level, default=0.2),
                    theano.Param(learning_rate, default=0.1)
                ],
                outputs=cost,
                updates=updates,
                givens={
                    self.x: pretrain_x,
                    self.is_train: is_train
                }
            )
            # append `fn` to the list of functions
            pretrain_fns.append(fn)

        return pretrain_fns

    def build_finetune_functions(self, learning_rate):
        
        is_train = T.iscalar('is_train')
        X = T.matrix('X', dtype='float32')
        AtRisk = T.ivector('AtRisk')
        Observed = T.ivector('Observed')
        #call the optimization function
        opt = Opt()
        
        test = theano.function(
            on_unused_input='ignore',
            inputs=[X, Observed, AtRisk, is_train] + self.masks,
            outputs=[self.riskLayer.cost(self.o, self.AtRisk), self.riskLayer.output, self.riskLayer.input],
            givens={
                self.x: X,
                self.o: Observed,
                self.AtRisk: AtRisk,
                self.is_train:is_train
            },
            name='test'
        )
        train = theano.function(
            on_unused_input='ignore',
            inputs=[X, Observed, AtRisk, is_train] + self.masks,
            outputs=[self.riskLayer.cost(self.o, self.AtRisk), self.riskLayer.output, self.riskLayer.input],
            updates=opt.SGD(self.riskLayer.cost(self.o, self.AtRisk) - self.L1 - self.L2_sqr, self.params, learning_rate),
            givens={
                self.x: X,
                self.o: Observed,
                self.AtRisk: AtRisk,
                self.is_train:is_train
            },
            name='train'
        )
        return test, train

    def reset_weight(self, params):
        for i in xrange(self.n_layers):
            self.hidden_layers[i].reset_weight((params[2*i], params[2*i+1]))
        self.riskLayer.reset_weight(params[-1])
       
    def reset_weight_by_rate(self, rate):
        for i in xrange(self.n_layers):
            self.hidden_layers[i].reset_weight_by_rate(rate)

    def update_layers(self):
        for l in self.hidden_layers:
            l.update_layer()

