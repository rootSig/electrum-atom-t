#!/usr/bin/env python
#
# Electrum - lightweight Bitcoin client
# Copyright (C) 2012 thomasv@ecdsa.org
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.


import threading, time, Queue, os, sys, shutil
from util import user_dir, appdata_dir, print_error
from bitcoin import *


class BlockchainVerifier(threading.Thread):
    """ Simple Payment Verification """

    def __init__(self, interface, config):
        threading.Thread.__init__(self)
        self.daemon = True
        self.config = config
        self.lock = threading.Lock()
        self.height = 0
        self.local_height = 0
        self.running = False
        self.headers_url = 'http://headers.electrum.org/blockchain_headers'
        self.interface = interface
        interface.register_channel('verifier')
        self.set_local_height()



    def start_interfaces(self):
        import interface
        servers = interface.DEFAULT_SERVERS
        servers = interface.filter_protocol(servers,'s')
        print_error("using %d servers"% len(servers))
        self.interfaces = map ( lambda server: interface.Interface({'server':server} ), servers )

        for i in self.interfaces:
            i.start()
            # subscribe to block headers
            i.register_channel('verifier')
            i.register_channel('get_header')
            i.send([ ('blockchain.headers.subscribe',[])], 'verifier')
            # note: each interface should send its results directly to a queue, instead of channels
            # pass the queue to the interface, so that several can share the same queue


    def get_new_response(self):
        # listen to interfaces, forward to verifier using the queue
        while self.is_running():
            for i in self.interfaces:
                try:
                    r = i.get_response('verifier',timeout=0)
                except Queue.Empty:
                    continue

                result = r.get('result')
                if result:
                    return (i,result)

            time.sleep(1)




    def stop(self):
        with self.lock: self.running = False
        #self.interface.poke('verifier')

    def is_running(self):
        with self.lock: return self.running


    def request_header(self, i, h):
        print_error("requesting header %d from %s"%(h, i.server))
        i.send([ ('blockchain.block.get_header',[h])], 'get_header')

    def retrieve_header(self, i):
        while True:
            try:
                r = i.get_response('get_header',timeout=1)
            except Queue.Empty:
                print_error('timeout')
                continue

            if r.get('error'):
                print_error('Verifier received an error:', r)
                continue

            # 3. handle response
            method = r['method']
            params = r['params']
            result = r['result']

            if method == 'blockchain.block.get_header':
                return result
                

    def get_chain(self, interface, final_header):

        header = final_header
        chain = [ final_header ]
        requested_header = False
        
        while self.is_running():

            if requested_header:
                header = self.retrieve_header(interface)
                if not header: return
                chain = [ header ] + chain
                requested_header = False

            height = header.get('block_height')
            previous_header = self.read_header(height -1)
            if not previous_header:
                self.request_header(interface, height - 1)
                requested_header = True
                continue

            # verify that it connects to my chain
            prev_hash = self.hash_header(previous_header)
            if prev_hash != header.get('prev_block_hash'):
                print_error("reorg")
                self.request_header(interface, height - 1)
                requested_header = True
                continue

            else:
                # the chain is complete
                return chain
                    
            
    def verify_chain(self, chain):

        first_header = chain[0]
        prev_header = self.read_header(first_header.get('block_height') -1)
        
        for header in chain:

            height = header.get('block_height')

            prev_hash = self.hash_header(prev_header)
            bits, target = self.get_target(height/2016)
            _hash = self.hash_header(header)
            try:
                assert prev_hash == header.get('prev_block_hash')
                assert bits == header.get('bits')
                assert eval('0x'+_hash) < target
            except:
                return False

            prev_header = header

        return True




    def verify_chunk(self, index, hexdata):
        data = hexdata.decode('hex')
        height = index*2016
        num = len(data)/80
        print_error("validating headers %d"%height)

        if index == 0:  
            previous_hash = ("0"*64)
        else:
            prev_header = self.read_header(index*2016-1)
            if prev_header is None: raise
            previous_hash = self.hash_header(prev_header)

        bits, target = self.get_target(index)

        for i in range(num):
            height = index*2016 + i
            raw_header = data[i*80:(i+1)*80]
            header = self.header_from_string(raw_header)
            _hash = self.hash_header(header)
            assert previous_hash == header.get('prev_block_hash')
            assert bits == header.get('bits')
            assert eval('0x'+_hash) < target

            previous_header = header
            previous_hash = _hash 

        self.save_chunk(index, data)


    def verify_header(self, header):
        # add header to the blockchain file
        # if there is a reorg, push it in a stack

        height = header.get('block_height')

        prev_header = self.read_header(height -1)
        if not prev_header:
            # return False to request previous header
            return False

        prev_hash = self.hash_header(prev_header)
        bits, target = self.get_target(height/2016)
        _hash = self.hash_header(header)
        try:
            assert prev_hash == header.get('prev_block_hash')
            assert bits == header.get('bits')
            assert eval('0x'+_hash) < target
        except:
            # this can be caused by a reorg.
            print_error("verify header failed"+ repr(header))
            verifier.undo_verifications()

            # return False to request previous header.
            return False

        self.save_header(header)
        print_error("verify header:", _hash, height)
        return True
        

            

    def header_to_string(self, res):
        s = int_to_hex(res.get('version'),4) \
            + rev_hex(res.get('prev_block_hash')) \
            + rev_hex(res.get('merkle_root')) \
            + int_to_hex(int(res.get('timestamp')),4) \
            + int_to_hex(int(res.get('bits')),4) \
            + int_to_hex(int(res.get('nonce')),4)
        return s


    def header_from_string(self, s):
        hex_to_int = lambda s: eval('0x' + s[::-1].encode('hex'))
        h = {}
        h['version'] = hex_to_int(s[0:4])
        h['prev_block_hash'] = hash_encode(s[4:36])
        h['merkle_root'] = hash_encode(s[36:68])
        h['timestamp'] = hex_to_int(s[68:72])
        h['bits'] = hex_to_int(s[72:76])
        h['nonce'] = hex_to_int(s[76:80])
        return h

    def hash_header(self, header):
        return rev_hex(Hash(self.header_to_string(header).decode('hex')).encode('hex'))

    def path(self):
        return os.path.join( self.config.path, 'blockchain_headers')

    def init_headers_file(self):
        filename = self.path()
        if os.path.exists(filename):
            return
        
        try:
            import urllib, socket
            socket.setdefaulttimeout(30)
            print_error("downloading ", self.headers_url )
            urllib.urlretrieve(self.headers_url, filename)
        except:
            print_error( "download failed. creating file", filename )
            open(filename,'wb+').close()

    def save_chunk(self, index, chunk):
        filename = self.path()
        f = open(filename,'rb+')
        f.seek(index*2016*80)
        h = f.write(chunk)
        f.close()
        self.set_local_height()

    def save_header(self, header):
        data = self.header_to_string(header).decode('hex')
        assert len(data) == 80
        height = header.get('block_height')
        filename = self.path()
        f = open(filename,'rb+')
        f.seek(height*80)
        h = f.write(data)
        f.close()
        self.set_local_height()


    def set_local_height(self):
        name = self.path()
        if os.path.exists(name):
            h = os.path.getsize(name)/80 - 1
            if self.local_height != h:
                self.local_height = h
                self.height = self.local_height


    def read_header(self, block_height):
        name = self.path()
        if os.path.exists(name):
            f = open(name,'rb')
            f.seek(block_height*80)
            h = f.read(80)
            f.close()
            if len(h) == 80:
                h = self.header_from_string(h)
                return h 


    def get_target(self, index):

        max_target = 0x00000000FFFF0000000000000000000000000000000000000000000000000000
        if index == 0: return 0x1d00ffff, max_target

        first = self.read_header((index-1)*2016)
        last = self.read_header(index*2016-1)
        
        nActualTimespan = last.get('timestamp') - first.get('timestamp')
        nTargetTimespan = 14*24*60*60
        nActualTimespan = max(nActualTimespan, nTargetTimespan/4)
        nActualTimespan = min(nActualTimespan, nTargetTimespan*4)

        bits = last.get('bits') 
        # convert to bignum
        MM = 256*256*256
        a = bits%MM
        if a < 0x8000:
            a *= 256
        target = (a) * pow(2, 8 * (bits/MM - 3))

        # new target
        new_target = min( max_target, (target * nActualTimespan)/nTargetTimespan )
        
        # convert it to bits
        c = ("%064X"%new_target)[2:]
        i = 31
        while c[0:2]=="00":
            c = c[2:]
            i -= 1

        c = eval('0x'+c[0:6])
        if c > 0x800000: 
            c /= 256
            i += 1

        new_bits = c + MM * i
        return new_bits, new_target




    def run(self):
        self.start_interfaces()
        
        self.init_headers_file()
        self.set_local_height()
        print_error( "blocks:", self.local_height )

        with self.lock:
            self.running = True

        while self.is_running():

            i, header = self.get_new_response()
            
            height = header.get('block_height')

            if height > self.local_height:
                # get missing parts from interface (until it connects to my chain)
                chain = self.get_chain( i, header )

                # skip that server if the result is not consistent
                if not chain: continue
                
                # verify the chain
                if self.verify_chain( chain ):
                    print_error("height:", height, i.server)
                    for header in chain:
                        self.save_header(header)
                        self.height = height
                else:
                    print_error("error", i.server)
                    # todo: dismiss that server

    



if __name__ == "__main__":
    import interface, simple_config
    
    config = simple_config.SimpleConfig({'verbose':True})

    i0 = interface.Interface()
    i0.start()

    bv = BlockchainVerifier(i0, config)
    bv.start()


    # listen to interfaces, forward to verifier using the queue
    while 1:
        time.sleep(1)



