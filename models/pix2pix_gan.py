from pathlib import Path

import numpy as np
import tensorflow as tf
from models.base_gan_model import BaseGanModel
from models.utils.loss_funcation import l1_loss
from utils.nii_utils import nii_header_reader, nii_writer


class Pix2PixGAN(BaseGanModel):
    def __init__(self, **kwargs):
        BaseGanModel.__init__(self, **kwargs)
        self._lambda = self.kwargs['model']['lambda']
        self.build_model()
        self.summary()
        self.saver = tf.train.Saver()

    def build_model(self):
        # train generator
        self.realA = tf.placeholder(tf.float32, [None, self.image_size[0], self.image_size[1], self.in_channels],
                                    name='realA')
        self.realB = tf.placeholder(tf.float32, [None, self.image_size[0], self.image_size[1], self.out_channels],
                                    name='realB')
        self.fakeB = self.generator(self.realA, name='generatorA2B')
        self.metricB = self.metrics_fn(self.fakeB, self.realB)

        fakeB_logit = self.discriminator(self.fakeB, name='discriminatorB')
        self.g_lossA2B = self.loss_fn(fakeB_logit, tf.ones_like(fakeB_logit)) + self._lambda * l1_loss(self.realB,
                                                                                                       self.fakeB)

        # train discriminator
        self.fakeB_sample = tf.placeholder(tf.float32, [None, self.image_size[0], self.image_size[1], self.in_channels],
                                           name='fakeB')
        realB_logit = self.discriminator(self.realB, reuse=True, name='discriminatorB')
        fakeB_logit = self.discriminator(self.fakeB_sample, reuse=True, name='discriminatorB')

        self.d_loss_realB = self.loss_fn(realB_logit, tf.ones_like(realB_logit))
        self.d_loss_fakeB = self.loss_fn(fakeB_logit, tf.zeros_like(fakeB_logit))
        self.d_lossB = self.d_loss_realB + self.d_loss_fakeB

        train_vars = tf.trainable_variables()
        self.g_vars = [var for var in train_vars if 'generator' in var.name]
        self.d_vars = [var for var in train_vars if 'discriminator' in var.name]

        # eval
        self.testA = tf.placeholder(tf.float32, [None, self.image_size[0], self.image_size[1], self.in_channels],
                                    name='testA')
        self.testB = tf.placeholder(tf.float32, [None, self.image_size[0], self.image_size[1], self.out_channels],
                                    name='testB')
        self.test_fakeB = self.generator(self.testA, reuse=True, name='generatorA2B')
        self.test_loss = l1_loss(self.testB, self.test_fakeB)
        self.test_metric = self.metrics_fn(self.test_fakeB, self.testB)

    def summary(self):
        realA_sum = tf.summary.image('{}/{}/{}/AReal'.format(self.dataset_name, self.name, self.tag), self.realA,
                                     max_outputs=1)
        offset = tf.ones_like(self.fakeB)
        fakeB = tf.add(self.fakeB, offset)
        value_min = tf.reduce_min(fakeB)
        fakeB = tf.subtract(fakeB, value_min)
        fakeB = tf.subtract(fakeB, offset)
        fakeB_sum = tf.summary.image('{}/{}/{}/BFake'.format(self.dataset_name, self.name, self.tag), fakeB,
                                     max_outputs=1)
        realB_sum = tf.summary.image('{}/{}/{}/BReal'.format(self.dataset_name, self.name, self.tag), self.realB,
                                     max_outputs=1)
        metric_sum = tf.summary.scalar('{}/{}/{}/metricB'.format(self.dataset_name, self.name, self.tag), self.metricB)
        g_loss_A2B_sum = tf.summary.scalar('{}/{}/{}/GLossA2B'.format(self.dataset_name, self.name, self.tag),
                                           self.g_lossA2B)
        self.g_sum = tf.summary.merge([g_loss_A2B_sum, realA_sum, realB_sum, fakeB_sum, metric_sum])

        d_loss_realB_sum = tf.summary.scalar('{}/{}/{}/DLossRealB'.format(self.dataset_name, self.name, self.tag),
                                             self.d_loss_realB)
        d_loss_fakeB_sum = tf.summary.scalar('{}/{}/{}/DLossFakeB'.format(self.dataset_name, self.name, self.tag),
                                             self.d_loss_fakeB)
        d_loss_B_sum = tf.summary.scalar('{}/{}/{}/DLossB'.format(self.dataset_name, self.name, self.tag), self.d_lossB)

        lr_sum = tf.summary.scalar('{}/{}/{}/LearningRate'.format(self.dataset_name, self.name, self.tag),
                                   self.lr_tensor)
        self.d_sum = tf.summary.merge([d_loss_realB_sum, d_loss_fakeB_sum, d_loss_B_sum, lr_sum])

        test_loss = tf.summary.scalar('{}/{}/{}/test_loss'.format(self.dataset_name, self.name, self.tag),
                                      self.test_loss)
        test_metric = tf.summary.scalar('{}/{}/{}/test_metric'.format(self.dataset_name, self.name, self.tag),
                                        self.test_metric)
        self.test_sum = tf.summary.merge([test_loss, test_metric])

    def train(self):
        """Train cyclegan"""
        g_optimizer = tf.train.AdamOptimizer(self.lr_tensor, beta1=0.5).minimize(self.g_lossA2B, var_list=self.g_vars)
        d_optimizer = tf.train.AdamOptimizer(self.lr_tensor, beta1=0.5).minimize(self.d_lossB, var_list=self.d_vars)

        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)
        writer = tf.summary.FileWriter('../tensorboard_logs/{}/{}/{}'.format(self.dataset_name, self.name, self.tag),
                                       self.sess.graph)
        data_generator = self.train_data_loader.get_data_generator()
        data_size = self.train_data_loader.get_size()

        eval_generator = self.test_data_loader.get_data_generator()
        best_eval_metric = float("inf")
        for epoch in range(self.epoch):
            lr = self.scheduler_fn(epoch)
            eval_metric = 0
            for step in range(data_size):
                a_path, batchA, b_path, batchB = next(data_generator)

                # Update G network and record fake outputs
                fakeB, _, g_sum, g_loss = self.sess.run([self.fakeB, g_optimizer, self.g_sum, self.g_lossA2B],
                                                        feed_dict={self.realA: batchA, self.realB: batchB,
                                                                   self.lr_tensor: lr})
                writer.add_summary(g_sum, epoch * data_size + step)

                # Update D network
                _, d_sum, d_loss = self.sess.run([d_optimizer, self.d_sum, self.d_lossB],
                                                 feed_dict={self.realB: batchB, self.fakeB_sample: fakeB,
                                                            self.lr_tensor: lr})
                writer.add_summary(d_sum, epoch * data_size + step)
                print('Epoch:{:>3d}/{:<3d} Step:{:>4d}/{:<4d} g_loss:{:<5.5f} d_loss:{:<5.5f}'.format(epoch, self.epoch,
                                                                                                      step, data_size,
                                                                                                      g_loss, d_loss))

                # eval G network
                a_path, batchA, b_path, batchB = next(eval_generator)
                test_metric, test_sum = self.sess.run([self.test_metric, self.test_sum],
                                                      feed_dict={self.testA: batchA, self.testB: batchB})
                writer.add_summary(test_sum, epoch * data_size + step)
                eval_metric += test_metric
            if eval_metric < best_eval_metric:
                self.save(self.checkpoint_dir, epoch, True)
            if epoch % self.save_freq == 0:
                self.save(self.checkpoint_dir, epoch, False)

    def test(self):
        init_op = tf.global_variables_initializer()
        self.sess.run(init_op)
        self.load(self.checkpoint_dir, False)
        data_generator = self.test_data_loader.get_data_generator()
        data_size = self.test_data_loader.get_size()
        pre_b_path = ''
        nii_model = list()
        sum_metric = 0
        sum_loss = 0
        for step in range(data_size):
            a_path, batchA, b_path, batchB = next(data_generator)
            fakeB, metric, loss = self.sess.run([self.test_fakeB, self.test_metric, self.test_loss],
                                                feed_dict={self.testA: batchA, self.testB: batchB})
            if pre_b_path != b_path:
                if pre_b_path != '':
                    b_nii_head = nii_header_reader(b_path)
                    nii_writer('./result/fake_{}.nii'.format(Path(b_path).stem), b_nii_head, np.array(nii_model))
                    print('Path:{} metric:{} loss:{}'.format(Path(b_path).stem, sum_metric, sum_loss))
                    sum_metric = 0
                    sum_loss = 0
                pre_b_path = b_path
            nii_model.append(np.squeeze(fakeB))
            sum_metric += metric
            sum_loss += loss
