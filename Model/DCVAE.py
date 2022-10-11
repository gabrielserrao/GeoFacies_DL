from keras import backend as K
from keras.applications.vgg16 import preprocess_input
from keras.applications.vgg16 import VGG16
from keras.callbacks import EarlyStopping, ReduceLROnPlateau, LearningRateScheduler, ModelCheckpoint
from keras.layers import Input, Dense, Lambda, Flatten, Reshape, merge, Activation, Add, AveragePooling2D
from keras.layers import Conv2D, Conv2DTranspose, Dropout, BatchNormalization, MaxPooling2D, UpSampling2D
from keras.layers import DepthwiseConv2D, Add, AveragePooling2D, Concatenate
from keras.layers import Conv3D, Conv3DTranspose, MaxPooling3D
from keras.layers.merge import Concatenate
from keras.models import Model
from keras.objectives import binary_crossentropy, MAE, MSE
from keras.optimizers import RMSprop, Adam

from keras_tqdm import TQDMNotebookCallback
import math
import numpy as np
import pandas as pd
import sys
import tensorflow as tf

from Model.Utils import kl_normal, kl_discrete, sampling_normal, EPSILON
from Model.BiLinearUp import BilinearUpsampling

class VAE():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(45,45,2),act='sigmoid',latent_dim=200,opt=RMSprop(),
        multi_GPU=0,hidden_dim=1024, deepL=(4096,2048),dropout=0):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.
            
        opt : Otimazer, method for otimization
        
        hidden_dim : int
            Dimension of hidden layer.

        filters : Array-like, shape (num_filters, num_filters, num_filters)
            Number of filters for each convolution in increasing order of
            depth.
        strides : Array-like, shape (num_filters, num_filters, num_filters)
            Number of strides for each convolution
            
        dropout : % de dropout [0,1]
        
        """
        self.act=act
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.deepL=deepL
        self.model = None
        self.input_shape =input_shape
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler =True
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)

        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler,TQDMNotebookCallback()]
        else:
            self.listCall=[self.earlystopper,self.reduce_lr,TQDMNotebookCallback()]

    def acc_pred(self,y_true, y_pred):         
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        epochs_drop = 20.0
        if (1+epoch)%epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

        
    def fit(self, x_train,x_v=None,num_epochs=1, batch_size=100, val_split=None,reset_model=True,verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if val_split is None:
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:    
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])


            if val_split is None:
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:              
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers        
        for i in range(len(self.deepL)):
            if i==0:
                Q = Flatten()(inputs)
                Q = Dense(self.deepL[i],activation='linear')(Q)
            else:
                Q = Dense(self.deepL[i],activation='linear')(Q)     
            Q = BatchNormalization()(Q)
            Q = Activation('relu')(Q)       
                       
        Q_5 = Dense(self.hidden_dim, activation='linear')
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        dp = Q_5(Q)
        hidden= Q_6(dp)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder =Model(inputs, z_mean)

        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        
        G_0 = Dense(self.hidden_dim, activation='linear')
        G_d = Dropout(self.dropout)
        G=[]
        for i in range(len(self.deepL)):
            G_ = Dense(self.deepL[len(self.deepL)-(1+i)])  
            G.append(G_)            
            G_ = BatchNormalization()
            G.append(G_)            
            G_ = Activation('linear')          
            G.append(G_)
                
        G_6 = Dense(np.prod(self.input_shape),activation=self.act)
        G_7 = Reshape(self.input_shape, name='generated')
        # Apply generator layers
        x = G_0(encoding)
        x = G_d(x)

        for i in range(len(G)):
            x = G[i](x)
        
        generated_ = G_6(x)
        generated = G_7(generated_)

        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_0(inputs_G)
        x = G_d(x)
        
        for i in range(len(G)):
            x = G[i](x)
        
        generated_G_ = G_6(x)
        generated_G = G_7(generated_G_)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)

    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * \
                                  binary_crossentropy(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))

class DCVAE_Curvas():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(45,45,2),act='sigmoid', KernelDim=(2,2,3,3),latent_dim=200,opt=RMSprop(),
        multi_GPU=0,hidden_dim=1024, filters=(2,64, 64, 64),strides=(1,1,1,1),dropout=0,loss=MAE):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.
            
        opt : Otimazer, method for otimization
        
        hidden_dim : int
            Dimension of hidden layer.

        filters : Array-like, shape (num_filters, num_filters, num_filters)
            Number of filters for each convolution in increasing order of
            depth.
        strides : Array-like, shape (num_filters, num_filters, num_filters)
            Number of strides for each convolution
            
        dropout : % de dropout [0,1]
        
        """
        self.loss=loss
        self.act=act
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.KernelDim=KernelDim
        self.model = None
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler =True
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)

        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler,TQDMNotebookCallback()]
        else:
            self.listCall=[self.earlystopper,self.reduce_lr,TQDMNotebookCallback()]


    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        epochs_drop = 20.0
        if (1+epoch)%epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

    def acc_pred(self,y_true, y_pred):           
        return MAE(y_true, y_pred)

        
    def fit(self, x_train,x_v=None,num_epochs=1, batch_size=100, val_split=None,reset_model=True,verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred,'mse'])
            if val_split is None:
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:    
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred,'mse'])
            if val_split is None:
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:              
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers        
        for i in range(len(self.filters)):
            if i==0:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], 1), 
                           strides=(self.strides[i], 1),padding='same',activation='linear')(inputs)        
            else:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], 1), padding='same',
                                         activation='linear',strides=(self.strides[i], 1))(Q)
            Q=BatchNormalization()(Q)
            Q=Activation('relu')(Q)  
                       
        Q_4 = Flatten()
        Q_5 = Dense(self.hidden_dim, activation='linear')
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        flat = Q_4(Q)
        dp = Q_5(flat)
        hidden= Q_6(dp)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder =Model(inputs, z_mean)

        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.ceil(self.input_shape[0] / np.prod(self.strides) )), int(np.ceil(self.input_shape[1] / np.prod(self.strides))), self.filters[-1])
        
        G_0 = Dense(self.hidden_dim, activation='linear')
        G_d = Dropout(self.dropout)
        G_1 = Dense(np.prod(out_shape), activation='linear')
        G_2 = Reshape(out_shape)
        G=[]
        for i in range(len(self.filters)):
            if i==0:
                G_ = Conv2DTranspose(self.filters[-1], (self.KernelDim[-1],1), 
                           strides=(self.strides[-1], 1),padding='same',activation='linear')              
            else:
                G_ = Conv2DTranspose(self.filters[-i-1], (self.KernelDim[-i-1],1), padding='same',
                                         activation='linear',strides=(self.strides[-i-1], 1))
            G.append(G_)
            G.append(BatchNormalization())
            G.append(Activation('relu'))
                
        G_5_= BilinearUpsampling(output_size=(self.input_shape[0], self.input_shape[1]))
        G_6 = Conv2D(self.input_shape[2], (2, 2), padding='same',
                     strides=(1, 1), activation=self.act, name='generated')
        # Apply generator layers
        x = G_0(encoding)
        x = G_d(x)
        x = G_1(x)
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated = G_6(x)
        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_0(inputs_G)
        x = G_1(x)
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated_G = G_6(x)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)
    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.loss(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))

class DCVAE():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(45,45,2),act='sigmoid', KernelDim=(2,2,3,3),latent_dim=200,opt=RMSprop(),isTerminal=False,
        filepath=None,multi_GPU=0,hidden_dim=1024, filters=(2,64, 64, 64),strides=(1,2,1,1),dropout=0,epochs_drop=20):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.
            
        opt : Otimazer, method for optimization
        
        hidden_dim : int
            Dimension of hidden layer.

        filters : Array-like, shape (num_filters, num_filters, num_filters)
            Number of filters for each convolution in increasing order of
            depth.
        strides : Array-like, shape (num_filters, num_filters, num_filters)
            Number of strides for each convolution
            
        dropout : % de dropout [0,1]
        
        """
        self.epochs_drop=epochs_drop
        self.act=act
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.KernelDim=KernelDim
        self.model = None
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler =True
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)
        self.filepath  = filepath

        if self.filepath is None:
        	self.ModelCheck = []
        else:
        	self.ModelCheck   = [ModelCheckpoint(self.filepath,verbose=0, save_best_only=True, save_weights_only=True,period=1)]
        if isTerminal:
        	nt=[]
        else:
        	nt=[TQDMNotebookCallback()]
        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler]+ self.ModelCheck+nt
        else:
            self.listCall=[self.earlystopper,self.reduce_lr]+ self.ModelCheck+nt

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        if (1+epoch)%self.epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

    def acc_pred(self,y_true, y_pred):           
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())

        
    def fit(self, x_train,x_v=None,num_epochs=1, batch_size=100, val_split=None,reset_model=True,verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if val_split is None:
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:    
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if val_split is None:
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:              
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)

        if self.filepath is not None:
            self.model.load_weights(self.filepath)


    def fit_generator (self, x_train, num_epochs=1, batch_size=100,reset_model=True, verbose=0, steps_per_epoch = 100,
                        val_set = None, validation_steps = None):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        if self.multi_GPU==0:

            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            self.history = self.model.fit_generator(
                                x_train,
                                steps_per_epoch = steps_per_epoch,
                                epochs=self.num_epochs,
                                verbose = verbose,
                                validation_data = val_set,
                                validation_steps = validation_steps,
                                callbacks=self.listCall,
                                workers = 0 ) 

        else:
            print("Function 'multi_gpu_model' not found")

        if self.filepath is not None:
            self.model.load_weights(self.filepath)


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers        
        for i in range(len(self.filters)):
            if i==0:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), 
                           strides=(self.strides[i], self.strides[i]),padding='same',activation='relu')(inputs)
            else:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), padding='same',
                                         activation='relu',strides=(self.strides[i], self.strides[i]))(Q)            
                       
        Q_4 = Flatten()
        Q_5 = Dense(self.hidden_dim, activation='relu')
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        flat = Q_4(Q)
        dp = Q_5(flat)
        hidden= Q_6(dp)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder =Model(inputs, z_mean)

        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.ceil(self.input_shape[0] / np.prod(self.strides) )), int(np.ceil(self.input_shape[1] / np.prod(self.strides))), self.filters[-1])
        
        G_0 = Dense(self.hidden_dim, activation='relu')
        G_d = Dropout(self.dropout)
        G_1 = Dense(np.prod(out_shape), activation='relu')
        G_2 = Reshape(out_shape)
        G=[]
        for i in range(len(self.filters)):
            if i==0:
                G_ = Conv2DTranspose(self.filters[-1], (self.KernelDim[-1], self.KernelDim[-1]), 
                           strides=(self.strides[-1], self.strides[-1]),padding='same',activation='relu')              
            else:
                G_ = Conv2DTranspose(self.filters[-i-1], (self.KernelDim[-i-1], self.KernelDim[-i-1]), padding='same',
                                         activation='relu',strides=(self.strides[-i-1], self.strides[-i-1]))
            G.append(G_)
                
        G_5_= BilinearUpsampling(output_size=(self.input_shape[0], self.input_shape[1]))
        G_6 = Conv2D(self.input_shape[2], (2, 2), padding='same',
                     strides=(1, 1), activation=self.act, name='generated')
        # Apply generator layers
        x = G_0(encoding)
        x = G_d(x)
        x = G_1(x)
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated = G_6(x)
        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_0(inputs_G)
        x = G_1(x)
        x = G_2(x)
        
        for i in range(len(self.filters)):
            x = G[i](x)
            
        x = G_5_(x)
        generated_G = G_6(x)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)
    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * \
                                  binary_crossentropy(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))

class DCVAE_Norm():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(45,45,2),act='sigmoid', KernelDim=(2,2,3,3),latent_dim=200,opt=RMSprop(),multi_GPU=0,isTerminal=False,
        filepath=None,hidden_dim=1024, filters=(2,64, 64, 64),strides=(1,2,1,1),dropout=0,epochs_drop=20):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.
            
        opt : Otimazer, method for otimization
        
        hidden_dim : int
            Dimension of hidden layer.

        filters : Array-like, shape (num_filters, num_filters, num_filters)
            Number of filters for each convolution in increasing order of
            depth.
        strides : Array-like, shape (num_filters, num_filters, num_filters)
            Number of strides for each convolution
            
        dropout : % de dropout [0,1]
        
        """
        self.epochs_drop=epochs_drop
        self.act=act
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.KernelDim=KernelDim
        self.model = None
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler=True        
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)
        self.filepath  = filepath

        if self.filepath is None:
        	self.ModelCheck = []
        else:
        	self.ModelCheck   = [ModelCheckpoint(self.filepath,verbose=0, save_best_only=True, save_weights_only=True,period=1)]
        if isTerminal:
        	nt=[]
        else:
        	nt=[TQDMNotebookCallback()]
        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler]+ self.ModelCheck+nt
        else:
            self.listCall=[self.earlystopper,self.reduce_lr]+ self.ModelCheck+nt
   

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        if (1+epoch)%self.epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

    def acc_pred(self,y_true, y_pred):           
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())

        
    def fit(self, x_train, x_v=None,num_epochs=1, batch_size=100, val_split=.1,reset_model=True,verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()
        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if val_split is None:
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:    
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if val_split is None:
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_data=(x_v,x_v))
            else:              
                self.history=self.modelGPU.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)

        if self.filepath is not None:
            self.model.load_weights(self.filepath)


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers        
        for i in range(len(self.filters)):
            if i==0:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), 
                           strides=(self.strides[i], self.strides[i]),padding='same')(inputs)
                Q = BatchNormalization()(Q)
                Q = Activation('relu')(Q)
            else:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), padding='same',
                                         strides=(self.strides[i], self.strides[i]))(Q)            
                Q = BatchNormalization()(Q)
                Q = Activation('relu')(Q)                
                       
        Q_4 = Flatten()
        Q_5 = Dense(self.hidden_dim)
        Q_50= BatchNormalization()
        Q_51=Activation('relu')
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        flat = Q_4(Q)
        db = Q_5(flat)
        da = Q_50(db)
        dp = Q_51(da)
        hidden= Q_6(dp)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder =Model(inputs, z_mean)

        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.ceil(self.input_shape[0] / np.prod(self.strides) )), int(np.ceil(self.input_shape[1] / np.prod(self.strides))), self.filters[-1])
        
        G_0 = Dense(self.hidden_dim)
        G_00= BatchNormalization()
        G_01= Activation('relu')
        G_d = Dropout(self.dropout)
        G_1 = Dense(np.prod(out_shape))
        G_10= BatchNormalization()
        G_11= Activation('relu')
        G_2 = Reshape(out_shape)
        G=[]
        for i in range(len(self.filters)):
            if i==0:
                G_ = Conv2DTranspose(self.filters[-1], (self.KernelDim[-1], self.KernelDim[-1]), 
                           strides=(self.strides[-1], self.strides[-1]),padding='same')
                G.append(G_)
                G_ = BatchNormalization()
                G.append(G_)
                G_ = Activation('relu')
                G.append(G_)                
            else:
                G_ = Conv2DTranspose(self.filters[-i-1], (self.KernelDim[-i-1], self.KernelDim[-i-1]), padding='same',
                                         strides=(self.strides[-i-1], self.strides[-i-1]))
                G.append(G_)
                G_ = BatchNormalization()
                G.append(G_)
                G_ = Activation('relu')
                G.append(G_)  
                
        G_5_= BilinearUpsampling(output_size=(self.input_shape[0], self.input_shape[1]))
        G_6 = Conv2D(self.input_shape[2], (2, 2), padding='same',
                     strides=(1, 1), activation=self.act, name='generated')
        # Apply generator layers
        x = G_0(encoding)
        x = G_00(x)
        x = G_01(x)
        x = G_d(x)
        x = G_1(x)
        x = G_10(x)        
        x = G_11(x)
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated = G_6(x)
        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_0(inputs_G)
        x = G_00(x)
        x = G_01(x)
        x = G_d(x)        
        x = G_1(x)
        x = G_10(x)        
        x = G_11(x)        
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated_G = G_6(x)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)
    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * \
                                  binary_crossentropy(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))

class DCVAE_NormV2():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(45,45,2),act='sigmoid', KernelDim=(2,2,3,3),latent_dim=200,opt=RMSprop(),multi_GPU=0,
        filters=(2,64, 64, 64),strides=(1,2,1,1),dropout=0):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.
            
        opt : Otimazer, method for otimization
        
        hidden_dim : int
            Dimension of hidden layer.

        filters : Array-like, shape (num_filters, num_filters, num_filters)
            Number of filters for each convolution in increasing order of
            depth.
        strides : Array-like, shape (num_filters, num_filters, num_filters)
            Number of strides for each convolution
            
        dropout : % de dropout [0,1]
        
        """
        self.act=act
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.KernelDim=KernelDim
        self.model = None
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler=True        
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)
        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler, TQDMNotebookCallback()]
        else:
            self.listCall=[self.earlystopper,self.reduce_lr, TQDMNotebookCallback()]

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        epochs_drop = 20.0
        if (1+epoch)%epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

    def acc_pred(self,y_true, y_pred):           
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())

        
    def fit(self, x_train, num_epochs=1, batch_size=100, val_split=.1,reset_model=True,verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()

        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            self.history=self.model.fit(x_train, x_train,
                       epochs=self.num_epochs,
                       batch_size=self.batch_size,
                       verbose=verbose,
                       shuffle=True,
                       callbacks=self.listCall,
                       validation_split=val_split)
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            self.history=self.modelGPU.fit(x_train, x_train,
                       epochs=self.num_epochs,
                       batch_size=self.batch_size,
                       verbose=verbose,
                       shuffle=True,
                       callbacks=self.listCall,
                       validation_split=val_split)


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers        
        for i in range(len(self.filters)):
            if i==0:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), 
                           strides=(self.strides[i], self.strides[i]),padding='same')(inputs)
                Q = BatchNormalization()(Q)
                Q = Activation('relu')(Q)
            else:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), padding='same',
                                         strides=(self.strides[i], self.strides[i]))(Q)            
                Q = BatchNormalization()(Q)
                Q = Activation('relu')(Q)                
                       
        Q_4 = Flatten()
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        flat = Q_4(Q)
        hidden= Q_6(flat)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder =Model(inputs, z_mean)

        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.ceil(self.input_shape[0] / np.prod(self.strides) )), int(np.ceil(self.input_shape[1] / np.prod(self.strides))), self.filters[-1])
        
        #G_0 = Dense(self.hidden_dim)
        #G_00= BatchNormalization()
        #G_01= Activation('relu')
        G_d = Dropout(self.dropout)
        G_1 = Dense(np.prod(out_shape))
        G_10= BatchNormalization()
        G_11= Activation('relu')
        G_2 = Reshape(out_shape)
        G=[]
        for i in range(len(self.filters)):
            if i==0:
                G_ = Conv2DTranspose(self.filters[-1], (self.KernelDim[-1], self.KernelDim[-1]), 
                           strides=(self.strides[-1], self.strides[-1]),padding='same')
                G.append(G_)
                G_ = BatchNormalization()
                G.append(G_)
                G_ = Activation('relu')
                G.append(G_)                
            else:
                G_ = Conv2DTranspose(self.filters[-i-1], (self.KernelDim[-i-1], self.KernelDim[-i-1]), padding='same',
                                         strides=(self.strides[-i-1], self.strides[-i-1]))
                G.append(G_)
                G_ = BatchNormalization()
                G.append(G_)
                G_ = Activation('relu')
                G.append(G_)  
                
        G_5_= BilinearUpsampling(output_size=(self.input_shape[0], self.input_shape[1]))
        G_6 = Conv2D(self.input_shape[2], (2, 2), padding='same',
                     strides=(1, 1), activation=self.act, name='generated')
        # Apply generator layers
        #x = G_0(encoding)
        #x = G_00(x)
        #x = G_01(x)
        x = G_d(encoding)
        x = G_1(x)
        x = G_10(x)        
        x = G_11(x)
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated = G_6(x)
        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        #x = G_0(inputs_G)
        #x = G_00(x)
        #x = G_01(x)
        x = G_d(inputs_G)        
        x = G_1(x)
        x = G_10(x)        
        x = G_11(x)        
        x = G_2(x)
        
        for i in range(len(G)):
            x = G[i](x)
            
        x = G_5_(x)
        generated_G = G_6(x)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)
    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * \
                                  binary_crossentropy(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))

class DCVAE_Inc():
    """
    Class to handle building and training Inception-VAE models.
    """
    def __init__(self, input_shape=(100, 100, 2), latent_dim=100,kernel_init=32,opt=Adam(amsgrad=True),drop=0.1,scheduler=False,filepath=None,epochs_drop=200):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.

        kernel_init : int
            Dimension of kernel.

        opt : Otimazer

        """
        self.initial_lrate=0.001
        self.epochs_drop=epochs_drop
        self.drop = drop
        self.opt = opt
        self.model = None
        self.generator = None
        self.filepath = filepath
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.kernel_init = kernel_init
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler =scheduler
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)
        
        if self.filepath is None:
        	self.ModelCheck = []
        else:
        	self.ModelCheck   = [ModelCheckpoint(self.filepath,verbose=0, save_best_only=True, save_weights_only=True,period=1)]

        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler, TQDMNotebookCallback()]+self.ModelCheck
        else:
            self.listCall=[self.earlystopper,self.reduce_lr, TQDMNotebookCallback()]+self.ModelCheck

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        if (1+epoch)%self.epochs_drop == 0:
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate


    def acc_pred(self,y_true, y_pred):
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())

    def fit(self, x_train,x_v=None, num_epochs=1, batch_size=100, val_split=.1,reset_model=True,verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()

        # Update parameters
        self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
        if x_v is None:
            self.history=self.model.fit(x_train, x_train,epochs=self.num_epochs,batch_size=self.batch_size,verbose=verbose,shuffle=True,
            callbacks=self.listCall,validation_split=val_split)
        else :
            self.history=self.model.fit(x_train, x_train,epochs=self.num_epochs,batch_size=self.batch_size,verbose=verbose,shuffle=True,
            callbacks=self.listCall,validation_data=(x_v,x_v))            

        if self.filepath is not None:
            self.model.load_weights(self.filepath)

    def fit_generator (self, x_train, num_epochs=1, batch_size=100, reset_model=True, verbose=0, steps_per_epoch = 100,
                        val_set = None, validation_steps = None):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
        self.history = self.model.fit_generator(x_train, steps_per_epoch = steps_per_epoch,
                            epochs=self.num_epochs, verbose = verbose,
                            validation_data = val_set, validation_steps = validation_steps,
                            callbacks=self.listCall,
                            workers = 0 )

        if self.filepath is not None:
            self.model.load_weights(self.filepath)

    def create_encoder_single_conv(self,in_chs, out_chs, kernel):
        assert kernel % 2 == 1
        model=Conv2D(out_chs, kernel_size=kernel, padding='same')(in_chs)
        model=BatchNormalization()(model)
        model=Activation('relu')(model)
        return model

    ## Encoder Inception Signle
    def create_encoder_inception_signle(self,in_chs, out_chs):
        channels = out_chs
        bn_ch = channels // 2
        bottleneck = self.create_encoder_single_conv(in_chs, bn_ch, 1)    
        conv1 = self.create_encoder_single_conv(bottleneck, channels, 1)
        conv3 = self.create_encoder_single_conv(bottleneck, channels, 3)
        conv5 = self.create_encoder_single_conv(bottleneck, channels, 5)
        conv7 = self.create_encoder_single_conv(bottleneck, channels, 7)
        pool3 = MaxPooling2D(3,strides=1,padding='SAME')(in_chs)
        pool5 = MaxPooling2D(5,strides=1,padding='SAME')(in_chs)
        return Add()([conv1,conv3,conv5,conv7,pool3,pool5])

    def create_downsampling_module(self,input_, pooling_kenel,filters):    
        av1  = AveragePooling2D(pool_size=pooling_kenel)(input_)
        cv1  = self.create_encoder_single_conv(av1,filters,1)   
        return cv1

    def create_decoder_single_conv(self,in_chs, out_chs, kernel,stride=1):
        model=Conv2DTranspose(out_chs, kernel_size=kernel,strides=stride, padding='same')(in_chs)
        model=BatchNormalization()(model)
        model=Activation('relu')(model)
        return model

    ## Decoder Inception Signle
    def create_decoder_inception_signle(self,in_chs, out_chs):
        channels = out_chs
        bn_ch = channels // 2
        bottleneck = self.create_decoder_single_conv(in_chs, bn_ch, 1)    
        conv1 = self.create_decoder_single_conv(bottleneck, channels, 1)
        conv3 = self.create_decoder_single_conv(bottleneck, channels, 3)
        conv5 = self.create_decoder_single_conv(bottleneck, channels, 5)
        conv7 = self.create_decoder_single_conv(bottleneck, channels, 7)
        pool3 = MaxPooling2D(3,strides=1,padding='SAME')(in_chs)
        pool5 = MaxPooling2D(5,strides=1,padding='SAME')(in_chs)
        return Add()([conv1,conv3,conv5,conv7,pool3,pool5])

    def create_upsampling_module(self,input_, pooling_kenel,filters):    
        cv1  = self.create_decoder_single_conv(input_,filters,pooling_kenel,stride=pooling_kenel)   
        return cv1

    def createEncoder(self,input_layer):
        upch1  = Conv2D(self.kernel_init, padding='same', kernel_size=1)(input_layer)
        stage1 = self.create_encoder_inception_signle(upch1,self.kernel_init)
        upch2  = self.create_downsampling_module(stage1,2,self.kernel_init*2)

        stage2 = self.create_encoder_inception_signle(upch2,self.kernel_init*2)
        upch3  = self.create_downsampling_module(stage2,2,self.kernel_init*4) 

        stage3 = self.create_encoder_inception_signle(upch3,self.kernel_init*4)
        upch4  = self.create_downsampling_module(stage3,4,self.kernel_init*8) 

        stage4 = self.create_encoder_inception_signle(upch4,self.kernel_init*8)
        out    = AveragePooling2D(self.input_shape[0]//16)(stage4)

        sq1 = Lambda(lambda x: K.squeeze(x, -2))(out)
        sq2 = Lambda(lambda x: K.squeeze(x, -2))(sq1)
        return sq2


        up1 = UpSampling3D((int(np.ceil(self.input_shape[0]/16))-1,int(np.ceil(self.input_shape[0]/16))-1,1))(sq3)

    def createDecoder(self,input_):

        sq1 = Lambda(lambda x: K.expand_dims(x, 1))(input_)
        sq2 = Lambda(lambda x: K.expand_dims(x, 1))(sq1)
        up1 = UpSampling2D(int(np.ceil(self.input_shape[0]/16)))(sq2)

        stage1 = self.create_decoder_inception_signle(up1,self.kernel_init*8)

        downch1 = self.create_upsampling_module(stage1,4,self.kernel_init*4)

        stage2  = self.create_decoder_inception_signle(downch1,self.kernel_init*4)
        downch2 = self.create_upsampling_module(stage2,2,self.kernel_init*2)

        stage3 = self.create_decoder_inception_signle(downch2,self.kernel_init*2)
        downch3 = self.create_upsampling_module(stage3,2,self.kernel_init)

        stage4 = self.create_decoder_inception_signle(downch3,self.kernel_init)
        stage5 = BilinearUpsampling(output_size=(self.input_shape[0], self.input_shape[1]))(stage4)
        last = Conv2DTranspose(self.input_shape[-1], kernel_size=1,activation='sigmoid')(stage5)
        return last


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)

        baseEncoder = self.createEncoder(inputs)
        baseEncoder = Dropout(self.drop)(baseEncoder)

        # Instantiate encoder layers
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(baseEncoder)
        z_log_var = Q_z_log_var(baseEncoder)
        self.encoder =Model(inputs, z_mean)

        # Sample from latent distributions

        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        
        G_0 = Dense(8*self.kernel_init)(encoding)
        G_0 = Dropout(self.drop)(G_0)
        baseDecoder = self.createDecoder(G_0)

        self.model =Model(inputs, baseDecoder)
        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var


        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        self.model.summary()
        print("Completed model setup.")
    def Encoder(self, x_test):
        return self.encoder.predict(x_test)

    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        if self.generator==None:
            # Set up generator
            inputs_G = Input(batch_shape=(None, self.latent_dim))
            G_0 = Dense(8*self.kernel_init)(inputs_G)
            G_0 = Dropout(self.drop)(G_0)
            generated = self.createDecoder(G_0)
            self.generator =Model(inputs_G, generated)
            for i,l in enumerate(self.model.layers):       
                if i > 92:        
                    self.generator.layers[i-92].set_weights(self.model.layers[i].get_weights())

            # Loss and optimizer do not matter here as we do not train these models
            self.generator.compile(optimizer=self.opt, loss='mse')

        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * \
                                  binary_crossentropy(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)

        return reconstruction_loss + kl_normal_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))

    def _sampling_concrete(self, args):
        """
        Sampling from a concrete distribution
        """
        return sampling_concrete(args, (None, self.latent_disc_dim))

class MobileNetVae():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(100,100,2),act='sigmoid', KernelDim=(3,3,3),latent_dim=200,opt=RMSprop(),multi_GPU=0,epochs_drop=200,
                 hidden_dim=1024, filters=(64, 64, 64),strides=(1,1,1),dropout=0,load_weights='',trainable=True,filepath = None):
        """
        Setting up everything.

        Parameters
        ----------
        input_shape : Array-like, shape (num_rows, num_cols, num_channels)
            Shape of image.

        latent_dim : int
            Dimension of latent distribution.
            
        opt : Otimazer, method for otimization
        
        hidden_dim : int
            Dimension of hidden layer.

        filters : Array-like, shape (num_filters, num_filters, num_filters)
            Number of filters for each convolution in increasing order of
            depth.
        strides : Array-like, shape (num_filters, num_filters, num_filters)
            Number of strides for each convolution
            
        dropout : % de dropout [0,1]
        
        """
        self.act=act
        self.load_weights=load_weights
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.epochs_drop= epochs_drop
        self.KernelDim=KernelDim
        self.model = None
        self.filepath = filepath
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.trainable=trainable
        self.scheduler =True
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)

        if self.filepath is None:
        	self.ModelCheck = []
        else:
        	self.ModelCheck   = [ModelCheckpoint(self.filepath,verbose=0, save_best_only=True, save_weights_only=True,period=1)]

        if self.scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler, TQDMNotebookCallback()]+self.ModelCheck
        else:
            self.listCall=[self.earlystopper,self.reduce_lr, TQDMNotebookCallback()]

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        if (1+epoch)%self.epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

    def relu6(self,x):      
        return K.relu(x, max_value=6)

    def acc_pred(self,y_true, y_pred):           
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())

    def _make_divisible(self,v, divisor, min_value=None):
        if min_value is None:
            min_value = divisor
        new_v = max(min_value, int(v + divisor / 2) // divisor * divisor)
        # Make sure that round down does not go down by more than 10%.
        if new_v < 0.9 * v:
            new_v += divisor
        return new_v

    def _inverted_res_block(self,inputs, expansion, stride, alpha, filters, block_id, skip_connection, rate=1):
        in_channels = inputs._keras_shape[-1]
        pointwise_conv_filters = int(filters * alpha)
        pointwise_filters = self._make_divisible(pointwise_conv_filters, 8)
        x = inputs
        prefix = 'ex_Cv_{}_'.format(block_id)
        if block_id:
            # Expand
            x = Conv2D(expansion * in_channels, kernel_size=1, padding='same',
                       use_bias=False, activation=None,
                       name=prefix + 'exd')(x)
            x = BatchNormalization(epsilon=1e-3, momentum=0.999,
                                   name=prefix + 'exd_BN')(x)
            x = Activation(self.relu6, name=prefix + 'exd_relu')(x)
        else:
            prefix = 'ex_Cv_'
        # Depthwise
        x = DepthwiseConv2D(kernel_size=3, strides=stride, activation=None,
                            use_bias=False, padding='same', dilation_rate=(rate, rate),
                            name=prefix + 'depthwise')(x)
        x = BatchNormalization(epsilon=1e-3, momentum=0.999,
                               name=prefix + 'depthwise_BN')(x)

        x = Activation(self.relu6, name=prefix + 'depthwise_relu')(x)

        # Project
        x = Conv2D(pointwise_filters,
                   kernel_size=1, padding='same', use_bias=False, activation=None,
                   name=prefix + 'projt')(x)
        x = BatchNormalization(epsilon=1e-3, momentum=0.999,
                               name=prefix + 'projt_BN')(x)

        if skip_connection:
            return Add(name=prefix + 'add')([inputs, x])

        # if in_channels == pointwise_filters and stride == 1:
        #    return Add(name='res_connect_' + str(block_id))([inputs, x])
        return x

        
    def fit(self, x_train=None, num_epochs=1, batch_size=100, val_split=.1,reset_model=True,verbose=0,x_v=None,
        generator=None,validation_data=None,steps_per_epoch=10,validation_steps=10):       
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()

        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if x_v is not None:
                self.history=self.model.fit(x_train,x_train,
                       epochs=self.num_epochs,
                       batch_size=self.batch_size,
                       shuffle=True,                       
                       verbose=verbose,
                       validation_data=(x_v,x_v),
                       callbacks=self.listCall)

            else:
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,
                           batch_size=self.batch_size,
                           verbose=verbose,
                           shuffle=True,
                           callbacks=self.listCall,
                           validation_split=val_split)
            
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if x_v is not None:
                self.history=self.modelGPU.fit(x_train,x_train,
                       epochs=self.num_epochs,
                       batch_size=self.batch_size,
                       shuffle=True,                       
                       verbose=verbose,
                       validation_data=(x_v,x_v),
                       callbacks=self.listCall)

            else:
                self.history=self.modelGPU.fit(x_train, x_train,
                       epochs=self.num_epochs,
                       batch_size=self.batch_size,
                       verbose=verbose,
                       shuffle=True,
                       callbacks=self.listCall,
                       validation_split=val_split)

        if self.filepath is not None:
            self.model.load_weights(self.filepath)

    def fit_generator (self, x_train, num_epochs=1, batch_size=100,reset_model=True, verbose=0, steps_per_epoch = 100,
                        val_set = None, validation_steps = None):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        if self.multi_GPU==0:
            
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            self.history = self.model.fit_generator(
                                x_train,
                                steps_per_epoch = steps_per_epoch,
                                epochs=self.num_epochs,
                                verbose = verbose,
                                validation_data = val_set,
                                validation_steps = validation_steps,
                                callbacks=self.listCall,
                                workers = 0 ) 

        else:
            print("Function 'multi_gpu_model' not found")

        if self.filepath is not None:
            self.model.load_weights(self.filepath)

    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        alpha=1
        OS=8
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers  based MobileNet     
        x = Conv2D(64,kernel_size=3,strides=(2, 2),padding='same',use_bias=False, name='Conv')(inputs)
        x = BatchNormalization(epsilon=1e-3, momentum=0.999, name='Conv_BN')(x)
        x = Activation(self.relu6, name='Conv_Relu6')(x)

        x = self._inverted_res_block(x, filters=16, alpha=alpha, stride=1,
                                    expansion=1, block_id=0, skip_connection=False)
        x = self._inverted_res_block(x, filters=24, alpha=alpha, stride=2,
                                expansion=6, block_id=1, skip_connection=False)
        x = self._inverted_res_block(x, filters=24, alpha=alpha, stride=1,
                                    expansion=6, block_id=2, skip_connection=True)
        x = self._inverted_res_block(x, filters=32, alpha=alpha, stride=2,
                                expansion=6, block_id=3, skip_connection=False)
        x = self._inverted_res_block(x, filters=32, alpha=alpha, stride=1,
                        expansion=6, block_id=4, skip_connection=True)
        x = self._inverted_res_block(x, filters=32, alpha=alpha, stride=1,
                                expansion=6, block_id=5, skip_connection=True)
        # stride in block 6 changed from 2 -> 1, so we need to use rate = 2
        x = self._inverted_res_block(x, filters=64, alpha=alpha, stride=1,  # 1!
                            expansion=6, block_id=6, skip_connection=False)
        x = self._inverted_res_block(x, filters=64, alpha=alpha, stride=1, rate=2,
                        expansion=6, block_id=7, skip_connection=True)
        x = self._inverted_res_block(x, filters=64, alpha=alpha, stride=1, rate=2,
                        expansion=6, block_id=8, skip_connection=True)
        x = self._inverted_res_block(x, filters=64, alpha=alpha, stride=1, rate=2,
                        expansion=6, block_id=9, skip_connection=True)

        x = self._inverted_res_block(x, filters=96, alpha=alpha, stride=1, rate=2,
                            expansion=6, block_id=10, skip_connection=False)
        x = self._inverted_res_block(x, filters=96, alpha=alpha, stride=1, rate=2,
                            expansion=6, block_id=11, skip_connection=True)
        x = self._inverted_res_block(x, filters=96, alpha=alpha, stride=1, rate=2,
                            expansion=6, block_id=12, skip_connection=True)

        x = self._inverted_res_block(x, filters=160, alpha=alpha, stride=1, rate=2,  # 1!
                            expansion=6, block_id=13, skip_connection=False)
        x = self._inverted_res_block(x, filters=160, alpha=alpha, stride=1, rate=4,
                            expansion=6, block_id=14, skip_connection=True)
        x = self._inverted_res_block(x, filters=160, alpha=alpha, stride=1, rate=4,
                            expansion=6, block_id=15, skip_connection=True)
    
        x = self._inverted_res_block(x, filters=320, alpha=alpha, stride=1, rate=4,
                            expansion=6, block_id=16, skip_connection=False)

        Q = AveragePooling2D(pool_size=(int(np.ceil(self.input_shape[0] / OS)), int(np.ceil(self.input_shape[1] / OS))))(x)
            
        Q_1 = Flatten()
        #Q_2 = Dense(self.hidden_dim)
        #Q_3= BatchNormalization()
        #Q_4= Activation('relu')

        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        #x = Q_1(Q)
        #x = Q_2(x)
        #x = Q_3(x)
        #hidden = Q_4(x)
        hidden = Q_1(Q)
        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder =Model(inputs, z_mean)


        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.Encoder = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.ceil(self.input_shape[0] / 4)),
                     int(np.ceil(self.input_shape[1] / 4)),10)
        
        #G_0 = Dense(self.hidden_dim)
        #G_1 = BatchNormalization()
        #G_2 = Activation('relu')
        G_3 = Dense(np.prod(out_shape))
        #G_4 = BatchNormalization()
        #G_5 = Activation('relu')
        G_6 = Reshape(out_shape)
        G_7 = Conv2DTranspose(16, (3, 3), padding='same',strides=1, activation='relu')
        G_8 = Conv2DTranspose(32, (3, 3), padding='same',strides=2, activation='relu')
        G_9 = Conv2DTranspose(32, (2, 2), padding='same',strides=2, activation='relu')
        G_10= BilinearUpsampling(output_size=(self.input_shape[0],self.input_shape[1])) 
        G_11= Conv2D(self.input_shape[2],3,padding='same', activation=self.act, name='generated')    

        # Apply generator layers
        #x = G_0(encoding)
        #x = G_1(x)
        #x = G_2(x)
        #x = G_3(x)
        x = G_3(encoding)        

        x = G_6(x)
        x = G_7(x)
        x = G_8(x)
        x = G_9(x)
        x = G_10(x)

        generated = G_11(x)

        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_3(inputs_G)
        #x = G_4(x)
        #x = G_5(x)
        x = G_6(x)
        x = G_7(x)
        x = G_8(x)
        x = G_9(x)
        x = G_10(x)
 
        generated_G = G_11(x)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.Encoder.predict(x_test)

    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * \
                                  binary_crossentropy(x, x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))
    
class DCVAE3D_V0():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models.
    """
    def __init__(self, input_shape=(100,100,10,3),act='sigmoid', KernelDim=(3,3,3),latent_dim=200,opt=RMSprop(),multi_GPU=0,isTerminal=False,
                 filters=(64, 64, 64),strides=[2,1,1], momentum=0.99,dropout=0.0,trainable=True,filepath=None,scheduler=True):
        self.momentum=momentum
        self.act=act
        self.multi_GPU=multi_GPU
        self.opt = opt
        self.KernelDim=KernelDim
        self.model = None
        self.input_shape = input_shape
        self.num_classes = input_shape[-1]
        self.latent_dim = latent_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr    = ReduceLROnPlateau(factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.filepath  = filepath
        if self.filepath is None:
        	self.ModelCheck = []
        else:
        	self.ModelCheck   = [ModelCheckpoint(self.filepath,verbose=0, save_best_only=True, save_weights_only=True,period=1)]
        if isTerminal:
        	nt=[]
        else:
        	nt=[TQDMNotebookCallback()]
        self.learningRateScheduler    = LearningRateScheduler(self.step_decay,verbose=0)
        if scheduler:
            self.listCall=[self.earlystopper,self.reduce_lr,self.learningRateScheduler]+ self.ModelCheck+nt
        else:
            self.listCall=[self.earlystopper,self.reduce_lr]+ self.ModelCheck+nt

    # learning rate schedule
    def step_decay(self,epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        epochs_drop = 20.0       
        if (1+epoch)%epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate=self.initial_lrate*drop
        else:
            lrate=self.initial_lrate

        return lrate

    def acc_pred(self,y_true, y_pred):
        return K.cast(K.equal(K.argmax(y_true, axis=-1),K.argmax(y_pred, axis=-1)),K.floatx())
        
    def fit(self, x_train=None, num_epochs=1, batch_size=100,val_split=None,reset_model=True,verbose=0,isGenerator=False,validation_data=None,steps_per_epoch=10,validation_steps=10):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()

        # Update parameters
        if self.multi_GPU==0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if isGenerator:
                self.history=self.model.fit_generator(x_train, steps_per_epoch=steps_per_epoch,
                       epochs=self.num_epochs,verbose=verbose,validation_data=validation_data,
                       validation_steps=validation_steps,
                       callbacks=self.listCall)                  
            else:
                self.history=self.model.fit(x_train, x_train,
                           epochs=self.num_epochs,batch_size=self.batch_size,
                           verbose=verbose,shuffle=True,validation_split=val_split,
                           callbacks=self.listCall)
            
        else:
            self.modelGPU=multi_gpu_model(self.model, gpus=self.multi_GPU)        
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            if isGenerator:
                self.history=self.modelGPU.fit_generator(x_train, steps_per_epoch=steps_per_epoch,
                       epochs=self.num_epochs,verbose=verbose,validation_data=validation_data,
                       validation_steps=validation_steps,
                       callbacks=self.listCall)

            else:
                self.history=self.modelGPU.fit(x_train, x_train,
                       epochs=self.num_epochs,batch_size=self.batch_size,
                       verbose=verbose,shuffle=True,validation_split=val_split,
                       callbacks=self.listCall)

        if self.filepath is not None:
        	self.model.load_weights(self.filepath)

    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs=inputs
        # Instantiate encoder layers        
        for i in range(len(self.filters)):
            if i==0:
                Q = Conv3D(self.filters[i], self.KernelDim[i], padding='same', strides=(self.strides[i], self.strides[i],1))(inputs)
            else:
                Q = Conv3D(self.filters[i], self.KernelDim[i],strides=self.strides[i], padding='same')(Q)
            Q = BatchNormalization(momentum=self.momentum)(Q)
            Q = Activation('relu')(Q)
            
        Q = Conv3D(32, (3, 3, 3),strides=(2,2,2), padding='valid')(Q)
        Q = BatchNormalization(momentum=self.momentum)(Q)
        Q = Activation('relu')(Q)  

        Q_4 = Flatten()
        #Q_5 = Dense(self.hidden_dim)
        #Q_50=BatchNormalization(momentum=self.momentum)
        #Q_51 = Activation('relu')
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder        
        flat = Q_4(Q)
        #db = Q_5(flat)
        #da = Q_50(db)
        #dp = Q_51(da)
        hidden= Q_6(flat)
        
        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        
        self.encoder =Model(inputs, z_mean)
        
        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.floor(self.input_shape[0] / np.prod(self.strides+[2,]))),
                     int(np.floor(self.input_shape[1] / np.prod(self.strides+[2,]))),
                     int(np.floor(self.input_shape[2] / np.prod(self.strides[1:]+[2,]))), 
                     32)
        
        #G_0 = Dense(self.hidden_dim)
        #G_00= BatchNormalization(momentum=self.momentum)
        #G_01 = Activation('relu')
        #G_d = Dropout(self.dropout)
        G_1 = Dense(np.prod(out_shape))
        #G_10= BatchNormalization(momentum=self.momentum)
        #G_11= LeakyReLU()
        #G_11 = Activation('relu')
        G_dd = Dropout(self.dropout)

        G_2 = Reshape(out_shape)
        
        G=[Conv3DTranspose(32,2,padding='valid',strides=2),BatchNormalization(momentum=self.momentum),Activation('relu')]
        for i in range(len(self.filters)):
            if i==0:                
                G_ = Conv3DTranspose(self.filters[-1], self.KernelDim[-1],
                                     padding='same',strides= self.strides[-1])  
            else:
                if len(self.filters)==(i+1):
                    kk=1
                else:
                    kk=self.strides[-i-1]
                pp='same'
                kp=self.KernelDim[-i-1]
                if i== 2:
                    pp='valid'
                    kp=(4,4,self.KernelDim[-i-1])
                G_ = Conv3DTranspose(self.filters[-i-1],kp, padding=pp,
                                     strides=(self.strides[-i-1], self.strides[-i-1], kk))
            G.append(G_)            
            G.append(BatchNormalization(momentum=self.momentum))
            G.append(Activation('relu'))
            
        #G_5_= BilinearUpsampling(output_size=(self.input_shape[0], self.input_shape[1],self.input_shape[2]))
        G_6 = Conv3D(self.input_shape[3],(3,3,3),padding='same', activation=self.act, name='generated')
        # Apply generator layers
        x = G_1(encoding)
        #x = G_00(x)
        #x = G_01(x)
        #x = G_d(x)
        #x = G_1(x)
        #x = G_10(x)
        #x = G_11(x)
        x = G_dd(x)
        x = G_2(x)

        for i in range(1+3*len(self.filters)):
            x = G[i](x)
            
     #   x = G_5_(x)
        generated = G_6(x)
        self.model =Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_1(inputs_G)
        #x = G_00(x)
        #x = G_01(x)
        #x = G_d(x)        
        #x = G_1(x)
        #x = G_10(x)
        #x = G_11(x)
        x = G_dd(x)
        x = G_2(x)
        
        for i in range(1+3*len(self.filters)):
            x = G[i](x)
            
        #x = G_5_(x)
        generated_G = G_6(x)
        self.generator = Model(inputs_G, generated_G)
                
        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)
    def Decoder(self,x_test,binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test),axis=-1)
        return self.model.predict(x_test)
        
    def generate(self, number_latent_sample=20,std=1,binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample=np.random.normal(0,std,size=(number_latent_sample,self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample),axis=-1)
        return self.generator.predict(latent_sample)

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x = K.flatten(x)
        x_generated = K.flatten(x_generated)
        #reconstruction_loss = self.input_shape[0] * self.input_shape[1] *  K.mean(K.binary_crossentropy(x,x_generated,from_logits=True),axis=-1)
        reconstruction_loss = self.input_shape[0] * self.input_shape[1] * binary_crossentropy(x,x_generated)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var)
        kl_disc_loss = 0
        return reconstruction_loss + kl_normal_loss + kl_disc_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))
        
class DCVAE_Style():
    """
    Class to handle building and training Deep Convolutional Variational Autoencoders models with Neural Transfer Style loss function.
    """

    def __init__(self, input_shape=(45, 45, 2), act='sigmoid', KernelDim=(2, 2, 3, 3), latent_dim=200, opt=RMSprop(), isTerminal=False,
                 filepath=None, multi_GPU=0, hidden_dim=1024, filters=(2, 64, 64, 64), strides=(1, 2, 1, 1), dropout=0, epochs_drop=20, style_weight=0,
                 kl_weight=0.5, reconstruction_weight=3600):

        self.epochs_drop = epochs_drop
        self.act = act
        self.multi_GPU = multi_GPU
        self.opt = opt
        self.KernelDim = KernelDim
        self.model = None
        self.input_shape = input_shape
        self.latent_dim = latent_dim
        self.hidden_dim = hidden_dim
        self.filters = filters
        self.strides = strides
        self.dropout = dropout
        self.earlystopper = EarlyStopping(patience=10, verbose=0)
        self.reduce_lr = ReduceLROnPlateau(
            factor=0.5, patience=5, min_lr=0.0000005, verbose=1)
        self.scheduler = True
        self.learningRateScheduler = LearningRateScheduler(
            self.step_decay, verbose=0)
        self.style_weight = style_weight
        self.reconstruction_weight = reconstruction_weight
        self.kl_weight = kl_weight
        self.filepath = filepath

        if self.filepath is None:
            self.ModelCheck = []
        else:
            self.ModelCheck = [ModelCheckpoint(
                self.filepath, verbose=0, save_best_only=True, save_weights_only=True, period=1)]
        if isTerminal:
            nt = []
        else:
            nt = [TQDMNotebookCallback()]
        if self.scheduler:
            self.listCall = [self.earlystopper, self.reduce_lr,
                             self.learningRateScheduler] + self.ModelCheck+nt
        else:
            self.listCall = [self.earlystopper,
                             self.reduce_lr] + self.ModelCheck+nt

        self.build_vgg_net()
        self.load_img_ref()

    # learning rate schedule
    def step_decay(self, epoch):
        self.initial_lrate = K.eval(self.model.optimizer.lr)
        drop = 0.8
        if (1+epoch) % self.epochs_drop == 0:
            #lrate = self.initial_lrate * math.pow(drop, math.floor((1+epoch)/epochs_drop))
            lrate = self.initial_lrate*drop
        else:
            lrate = self.initial_lrate

        return lrate

    def acc_pred(self, y_true, y_pred):
        if self.input_shape[-1] != 1:
            return K.cast(K.equal(K.argmax(y_true, axis=-1), K.argmax(y_pred, axis=-1)), K.floatx())
        th = 0.5
        if self.act == 'tanh':
            th = 0.0

        y_true = K.switch(K.greater(y_true, th), K.ones_like(
            y_true), K.zeros_like(y_true))
        y_pred = K.switch(K.greater(y_pred, th), K.ones_like(
            y_pred), K.zeros_like(y_pred))

        return K.cast(K.equal(y_true, y_pred), K.floatx())

    def fit(self, x_train, x_v=None, num_epochs=1, batch_size=100, val_split=None, reset_model=True, verbose=0):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()

        # Update parameters
        if self.multi_GPU == 0:
            self.model.compile(optimizer=self.opt, loss=self._vae_loss, metrics=[
                               self.acc_pred, self.sum_style_loss])
            if val_split is None:
                self.history = self.model.fit(x_train, x_train,
                                              epochs=self.num_epochs,
                                              batch_size=self.batch_size,
                                              verbose=verbose,
                                              shuffle=True,
                                              callbacks=self.listCall,
                                              validation_data=(x_v, x_v))
            else:
                self.history = self.model.fit(x_train, x_train,
                                              epochs=self.num_epochs,
                                              batch_size=self.batch_size,
                                              verbose=verbose,
                                              shuffle=True,
                                              callbacks=self.listCall,
                                              validation_split=val_split)
        else:
            self.modelGPU = multi_gpu_model(self.model, gpus=self.multi_GPU)
            self.modelGPU.compile(optimizer=self.opt, loss=self._vae_loss, metrics=[
                                  self.acc_pred, self.sum_style_loss])
            if val_split is None:
                self.history = self.modelGPU.fit(x_train, x_train,
                                                 epochs=self.num_epochs,
                                                 batch_size=self.batch_size,
                                                 verbose=verbose,
                                                 shuffle=True,
                                                 callbacks=self.listCall,
                                                 validation_data=(x_v, x_v))
            else:
                self.history = self.modelGPU.fit(x_train, x_train,
                                                 epochs=self.num_epochs,
                                                 batch_size=self.batch_size,
                                                 verbose=verbose,
                                                 shuffle=True,
                                                 callbacks=self.listCall,
                                                 validation_split=val_split)

        if self.filepath is not None:
            self.model.load_weights(self.filepath)


    def fit_generator (self, x_train, num_epochs=1, batch_size=100,reset_model=True, verbose=0, steps_per_epoch = 100,
                        val_set = None, validation_steps = None):
        """
        Training model
        """
        self.batch_size = batch_size
        self.num_epochs = num_epochs
        if reset_model:
            self._set_model()        

        # Update parameters
        if self.multi_GPU==0:

            self.model.compile(optimizer=self.opt, loss=self._vae_loss,metrics=[self.acc_pred])
            self.history = self.model.fit_generator(
                                x_train,
                                steps_per_epoch = steps_per_epoch,
                                epochs=self.num_epochs,
                                verbose = verbose,
                                validation_data = val_set,
                                validation_steps = validation_steps,
                                callbacks=self.listCall,
                                workers = 0 ) 

        else:
            print("Function 'multi_gpu_model' not found")

        if self.filepath is not None:
            self.model.load_weights(self.filepath)


    def _set_model(self):
        """
        Setup model (method should only be called in self.fit())
        """
        print("Setting up model...")
        # Encoder
        inputs = Input(batch_shape=(None,) + self.input_shape)
        self.inputs = inputs
        # Instantiate encoder layers
        for i in range(len(self.filters)):
            if i == 0:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]),
                           strides=(self.strides[i], self.strides[i]), padding='same', activation='relu')(inputs)
            else:
                Q = Conv2D(self.filters[i], (self.KernelDim[i], self.KernelDim[i]), padding='same',
                           activation='relu', strides=(self.strides[i], self.strides[i]))(Q)

        Q_4 = Flatten()
        Q_5 = Dense(self.hidden_dim, activation='relu')
        Q_6 = Dropout(self.dropout)
        Q_z_mean = Dense(self.latent_dim)
        Q_z_log_var = Dense(self.latent_dim)

        # Set up encoder
        flat = Q_4(Q)
        dp = Q_5(flat)
        hidden = Q_6(dp)

        # Parameters for continous latent distribution
        z_mean = Q_z_mean(hidden)
        z_log_var = Q_z_log_var(hidden)
        self.encoder = Model(inputs, z_mean)

        # Sample from latent distributions
        encoding = Lambda(self._sampling_normal, output_shape=(
            self.latent_dim,))([z_mean, z_log_var])
        self.encoding = encoding
        # Generator
        # Instantiate generator layers to be able to sample from latent
        # distribution later
        out_shape = (int(np.ceil(self.input_shape[0] / np.prod(self.strides))), int(
            np.ceil(self.input_shape[1] / np.prod(self.strides))), self.filters[-1])

        G_0 = Dense(self.hidden_dim, activation='relu')
        G_d = Dropout(self.dropout)
        G_1 = Dense(np.prod(out_shape), activation='relu')
        G_2 = Reshape(out_shape)
        G = []
        for i in range(len(self.filters)):
            if i == 0:
                G_ = Conv2DTranspose(self.filters[-1], (self.KernelDim[-1], self.KernelDim[-1]),
                                     strides=(self.strides[-1], self.strides[-1]), padding='same', activation='relu')
            else:
                G_ = Conv2DTranspose(self.filters[-i-1], (self.KernelDim[-i-1], self.KernelDim[-i-1]), padding='same',
                                     activation='relu', strides=(self.strides[-i-1], self.strides[-i-1]))
            G.append(G_)

        G_5_ = BilinearUpsampling(output_size=(
            self.input_shape[0], self.input_shape[1]))
        G_6 = Conv2D(self.input_shape[2], (2, 2), padding='same',
                     strides=(1, 1), activation=self.act, name='generated')
        # Apply generator layers
        x = G_0(encoding)
        x = G_d(x)
        x = G_1(x)
        x = G_2(x)

        for i in range(len(G)):
            x = G[i](x)

        x = G_5_(x)
        generated = G_6(x)
        self.model = Model(inputs, generated)
        # Set up generator
        inputs_G = Input(batch_shape=(None, self.latent_dim))
        x = G_0(inputs_G)
        x = G_1(x)
        x = G_2(x)

        for i in range(len(self.filters)):
            x = G[i](x)

        x = G_5_(x)
        generated_G = G_6(x)
        self.generator = Model(inputs_G, generated_G)

        # Store latent distribution parameters
        self.z_mean = z_mean
        self.z_log_var = z_log_var

        # Compile models
        #self.opt = RMSprop()
        self.model.compile(optimizer=self.opt, loss=self._vae_loss)
        # Loss and optimizer do not matter here as we do not train these models
        self.generator.compile(optimizer=self.opt, loss='mse')
        self.model.summary()
        print("Completed model setup.")

    def Encoder(self, x_test):
        """
        Return predicted result from the encoder model
        """
        return self.encoder.predict(x_test)

    def Decoder(self, x_test, binary=False):
        """
        Return predicted result from the DCVAE model
        """
        if binary:
            return np.argmax(self.model.predict(x_test), axis=-1)
        return self.model.predict(x_test)

    def generate(self, number_latent_sample=20, std=1, binary=False):
        """
        Generating examples from samples from the latent distribution.
        """
        latent_sample = np.random.normal(
            0, std, size=(number_latent_sample, self.latent_dim))
        if binary:
            return np.argmax(self.generator.predict(latent_sample), axis=-1)
        return self.generator.predict(latent_sample)

    def F_matrix(self, phi):
        F = K.reshape(
            phi, (-1, K.shape(phi)[-3] * K.shape(phi)[-2], K.shape(phi)[-1]))

        return F

    def gray2rgb(self, img):
        if self.act == 'tanh':
            rgb_img = 127.5 + 127.5*K.repeat_elements(img, 3, axis=-1)
        else:
            rgb_img = 255*K.repeat_elements(img, 3, axis=-1)
        return rgb_img

    def gray2rgb_ref(self, img):
        rgb_img = 255*K.repeat_elements(img, 3, axis=-1)
        return rgb_img

    def gram_matrix(self, F):
        Nc = K.cast(K.shape(F)[-2], dtype='float32')
        Nz = K.cast(K.shape(F)[-1], dtype='float32')
        Z = 1.0 / (Nc * Nz)
        G = Z * tf.matmul(F, F, transpose_a=True)
        return G

    def compute_F_matrices(self, phi1, phi2, phi3, phi4):
        F1 = self.F_matrix(phi1)
        F2 = self.F_matrix(phi2)
        F3 = self.F_matrix(phi3)
        F4 = self.F_matrix(phi4)
        return F1, F2, F3, F4

    def compute_gram_matrices(self, F1, F2, F3, F4):
        G1 = self.gram_matrix(F1)
        G2 = self.gram_matrix(F2)
        G3 = self.gram_matrix(F3)
        G4 = self.gram_matrix(F4)
        return G1, G2, G3, G4

    def load_img_ref(self):
        img_ref = pd.read_csv('DataSet/Referencia', sep=",")
        self.img_ref = img_ref.values.astype('uint8').T
        input_img_ref = K.placeholder(
            dtype=tf.float32, name="input_img_ref", shape=(1, 250, 250, 1))
        rgb_img_ref = self.gray2rgb_ref(input_img_ref)
        phi1_fw_pca, phi2_fw_pca, phi3_fw_pca, phi4_fw_pca = self.vgg_net(
            self.preprocessing_image(rgb_img_ref))
        F1_fw_pca, F2_fw_pca, F3_fw_pca, F4_fw_pca = self.compute_F_matrices(
            phi1_fw_pca, phi2_fw_pca, phi3_fw_pca, phi4_fw_pca)
        G1_fw_pca, G2_fw_pca, G3_fw_pca, G4_fw_pca = self.compute_gram_matrices(
            F1_fw_pca, F2_fw_pca, F3_fw_pca, F4_fw_pca)
        sess = K.get_session()
        self.G1, self.G2, self.G3, self.G4 = sess.run([G1_fw_pca, G2_fw_pca, G3_fw_pca, G4_fw_pca], feed_dict={
                                                      input_img_ref: self.img_ref.reshape(1, 250, 250, 1).astype('float32')})

    def build_vgg_net(self):
        model_base = VGG16(weights='imagenet', include_top=True)
        Fmap1 = model_base.get_layer("block1_conv2").output
        Fmap2 = model_base.get_layer("block2_conv2").output
        Fmap3 = model_base.get_layer("block3_conv3").output
        Fmap4 = model_base.get_layer("block4_conv3").output

        model_vgg = Model(inputs=model_base.input, outputs=[
                          Fmap1, Fmap2, Fmap3, Fmap4])
        model_vgg.trainable = False
    #   model_vgg.summary()
        self.vgg_net = model_vgg

    def style_loss(self, G, A):
        Nz = K.cast(K.shape(G)[-1], dtype='float32')
        Z = (1.0/(Nz**2))
        loss = Z * tf.reduce_mean(K.sum(K.square(G - A), axis=(-1, -2)))
        return loss

    def preprocessing_image(self, img):
        normalized_rgb_img = Lambda(lambda x: preprocess_input(x))(img)
        return normalized_rgb_img

    def total_variation_loss(self, fw_image_pred, fw_image):
        loss = tf.reduce_sum(
            tf.image.total_variation(fw_image, name='total_variation'))
        return loss

    def content_loss(self, F2_pca, F2_fw_pca):
        Nc = K.cast(K.shape(F2_pca)[-2], dtype='float32')
        Nz = K.cast(K.shape(F2_pca)[-1], dtype='float32')
        Z = 1.0 / (Nc * Nz)
        loss = Z * \
            tf.reduce_mean(K.sum(K.square(F2_pca - F2_fw_pca), axis=(-1, -2)))
        return loss

    def sum_style_loss(self, x_pred, x):
        rgb_fw_pca = self.gray2rgb(x)
        phi1_fw_pca, phi2_fw_pca, phi3_fw_pca, phi4_fw_pca = self.vgg_net(
            self.preprocessing_image(rgb_fw_pca))

        F1_fw_pca, F2_fw_pca, F3_fw_pca, F4_fw_pca = self.compute_F_matrices(
            phi1_fw_pca, phi2_fw_pca, phi3_fw_pca, phi4_fw_pca)
        G1_fw_pca, G2_fw_pca, G3_fw_pca, G4_fw_pca = self.compute_gram_matrices(
            F1_fw_pca, F2_fw_pca, F3_fw_pca, F4_fw_pca)

        loss_G1 = self.style_loss(G1_fw_pca, self.G1)
        loss_G2 = self.style_loss(G2_fw_pca, self.G2)
        loss_G3 = self.style_loss(G3_fw_pca, self.G3)
        loss_G4 = self.style_loss(G4_fw_pca, self.G4)
        loss_style = loss_G1 + loss_G2 + loss_G3 + loss_G4

        return loss_style

    def _vae_loss(self, x, x_generated):
        """
        Variational Auto Encoder loss.
        """
        x_ = K.flatten(x)
        x_generated_fl = K.flatten(x_generated)

        if self.act == 'tanh':
            reconstruction_loss = self.reconstruction_weight * \
                MSE(x_, x_generated_fl)
        else:
            reconstruction_loss = self.reconstruction_weight * \
                binary_crossentropy(x_, x_generated_fl)
        kl_normal_loss = kl_normal(self.z_mean, self.z_log_var, weight=self.kl_weight)

        x_gen_style = self.sum_style_loss([], x_generated)
        tv_loss = self.total_variation_loss([], x_generated)
        return reconstruction_loss + kl_normal_loss + (x_gen_style)*self.style_weight+(2e-2)*tv_loss

    def _sampling_normal(self, args):
        """
        Sampling from a normal distribution.
        """
        z_mean, z_log_var = args
        return sampling_normal(z_mean, z_log_var, (None, self.latent_dim))
