import time
import numpy as np
import tensorflow as tf
import argparse
import os
import thop
from models import SpHGAT
from utils import process
from sklearn.metrics import f1_score


def main(args):
    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    config = tf.ConfigProto()
    config.gpu_options.allow_growth = True
    dataset = args.dataset
    # training params
    batch_size = 1
    nb_epochs = 100000
    patience = 100
    lr = args.lr  
    l2_coef = args.l2  
    hid_units = [args.units]  
    n_heads = [1, 1] # layers
    drop_out = args.drop
    residual = False
    nonlinearity = tf.nn.elu
    model = SpHGAT

    print('Dataset: ' + dataset)
    print('----- Opt. hyperparams -----')
    print('lr: ' + str(lr))
    print('l2_coef: ' + str(l2_coef))

    sparse = True

    adj, features, y_train, y_val, y_test, train_mask, val_mask, test_mask = process.load_data(dataset)
    features, spars = process.preprocess_features(features)

    nb_nodes = features.shape[0]
    ft_size = features.shape[1]
    nb_classes = y_train.shape[1]

    features = features[np.newaxis]
    y_train = y_train[np.newaxis]
    y_val = y_val[np.newaxis]
    y_test = y_test[np.newaxis]
    train_mask = train_mask[np.newaxis]
    val_mask = val_mask[np.newaxis]
    test_mask = test_mask[np.newaxis]

    if sparse:
        biases = process.preprocess_adj_bias(adj)
    else:
        adj = adj.todense()
        adj = adj[np.newaxis]
        biases = process.adj_to_bias(adj, [nb_nodes], nhood=1)

    with tf.Graph().as_default():
        with tf.name_scope('input'):
            ftr_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, nb_nodes, ft_size))
            if sparse:
                bias_in = tf.sparse_placeholder(dtype=tf.float32)
            else:
                bias_in = tf.placeholder(dtype=tf.float32, shape=(batch_size, nb_nodes, nb_nodes))
            lbl_in = tf.placeholder(dtype=tf.int32, shape=(batch_size, nb_nodes, nb_classes))
            msk_in = tf.placeholder(dtype=tf.int32, shape=(batch_size, nb_nodes))

            total_params, total_flops = thop.profile(model, inputs=[ftr_in, lbl_in, msk_in])
            print("Total number of model parameters: {:.2f}M".format(total_params / 1e6))
            print("Total number of FLOPs: {:.2f}M".format(total_flops / 1e6))

            attn_drop = tf.placeholder(dtype=tf.float32, shape=())
            ffd_drop = tf.placeholder(dtype=tf.float32, shape=())
            is_train = tf.placeholder(dtype=tf.bool, shape=())


        logits = model.inference(ftr_in, nb_classes, nb_nodes, is_train,
                                    attn_drop, ffd_drop,
                                    bias_mat=bias_in,
                                    hid_units=hid_units, n_heads=n_heads,
                                    activation=nonlinearity)
        log_resh = tf.reshape(logits, [-1, nb_classes])
        lab_resh = tf.reshape(lbl_in, [-1, nb_classes])
        msk_resh = tf.reshape(msk_in, [-1])
        loss = model.masked_softmax_cross_entropy(log_resh, lab_resh, msk_resh)
        accuracy = model.masked_accuracy(log_resh, lab_resh, msk_resh)

        pred_all = tf.cast(tf.argmax(log_resh, 1), dtype=tf.int32)
        real_all = tf.cast(tf.argmax(lab_resh, 1), dtype=tf.int32)
        train_op = model.my_training(loss, lr, l2_coef)



        init_op = tf.group(tf.global_variables_initializer(), tf.local_variables_initializer())


        vlss_mn = np.inf
        vacc_mx = 0.0
        tlss_mn = 0.0
        tacc_mx = 0.0
        curr_step = 0
        with tf.Session(config=config) as sess:
            sess.run(init_op)
            train_loss_avg = 0
            train_acc_avg = 0
            val_loss_avg = 0
            val_acc_avg = 0

            for epoch in range(nb_epochs):
                tr_step = 0
                tr_size = features.shape[0]

                while tr_step * batch_size < tr_size:
                    if sparse:
                        bbias = biases
                    else:
                        bbias = biases[tr_step*batch_size:(tr_step+1)*batch_size]

                    _, loss_value_tr, acc_tr = sess.run([train_op, loss, accuracy],
                        feed_dict={
                            ftr_in: features[tr_step*batch_size:(tr_step+1)*batch_size],
                            bias_in: bbias,
                            lbl_in: y_train[tr_step*batch_size:(tr_step+1)*batch_size],
                            msk_in: train_mask[tr_step*batch_size:(tr_step+1)*batch_size],
                            is_train: True,
                            attn_drop: drop_out, ffd_drop: drop_out})
                    train_loss_avg += loss_value_tr
                    train_acc_avg += acc_tr
                    tr_step += 1

                vl_step = 0
                vl_size = features.shape[0]

                while vl_step * batch_size < vl_size:
                    if sparse:
                        bbias = biases
                    else:
                        bbias = biases[vl_step*batch_size:(vl_step+1)*batch_size]
                    loss_value_vl, acc_vl = sess.run([loss, accuracy],
                        feed_dict={
                            ftr_in: features[vl_step*batch_size:(vl_step+1)*batch_size],
                            bias_in: bbias,
                            lbl_in: y_val[vl_step*batch_size:(vl_step+1)*batch_size],
                            msk_in: val_mask[vl_step*batch_size:(vl_step+1)*batch_size],
                            is_train: False,
                            attn_drop: 0.0, ffd_drop: 0.0})
                    val_loss_avg += loss_value_vl
                    val_acc_avg += acc_vl
                    vl_step += 1


                ts_size = features.shape[0]
                ts_step = 0
                ts_loss = 0.0
                ts_acc = 0.0

                print(epoch, 'Training: loss = %.5f, acc = %.5f | Val: loss = %.5f, acc = %.5f' %
                        (train_loss_avg/tr_step, train_acc_avg/tr_step,
                        val_loss_avg/vl_step, val_acc_avg/vl_step))

                if val_acc_avg/vl_step >= vacc_mx or val_loss_avg/vl_step <= vlss_mn:
                    vacc_mx = np.max((val_acc_avg/vl_step, vacc_mx))
                    vlss_mn = np.min((val_loss_avg/vl_step, vlss_mn))
                    curr_step = 0

                    while ts_step * batch_size < ts_size:
                        if sparse:
                            bbias = biases
                        else:
                            bbias = biases[ts_step * batch_size:(ts_step + 1) * batch_size]
                        loss_value_ts, acc_ts = sess.run(
                            [loss, accuracy],
                            feed_dict={
                                ftr_in: features[ts_step * batch_size:(ts_step + 1) * batch_size],
                                bias_in: bbias,
                                lbl_in: y_test[ts_step * batch_size:(ts_step + 1) * batch_size],
                                msk_in: test_mask[ts_step * batch_size:(ts_step + 1) * batch_size],
                                is_train: False,
                                attn_drop: 0.0, ffd_drop: 0.0})
                        ts_loss += loss_value_ts
                        ts_acc += acc_ts
                        ts_step += 1
                    tlss_mn = ts_loss / ts_step
                    tacc_mx = ts_acc / ts_step
                else:
                    curr_step += 1
                    if curr_step == patience:
                        print('Early stop! Min validation loss: ', vlss_mn, ', Max accuracy: ', vacc_mx)
                        print('Test loss:', tlss_mn, ', accuracy {}'.format(tacc_mx))
                        break
                train_loss_avg = 0
                train_acc_avg = 0
                val_loss_avg = 0
                val_acc_avg = 0
            sess.close()

def parse_args():
    parser = argparse.ArgumentParser(description='run LGCN')
    parser.add_argument('-gpu', nargs = '?', default='0', help='the ID for GPU')
    parser.add_argument('-dataset', nargs = '?', default='cora', help='name of dataset')
    parser.add_argument('-lr', default=0.005, type=float, help='learning rate')
    parser.add_argument('-l2', default=0.0001, type=float, help='weight_decay')
    parser.add_argument('-units', default=64, type=int, help='dimension for hidden unit')
    parser.add_argument('-drop', default=0.2, type=float, help='drop out')
    return parser.parse_args()

if __name__ == '__main__':
    args = parse_args()
    main(args)
