import logging

# Import functions data structures used by Out of Order Scheduler.
from helpers import *


# Main scheduler class.
class out_of_order_scheduler:


    def __init__ (self, infilename, outfilename):

        # Constant for this project.
        ARCH_REGS_COUNT = 32

        # Parse input file. Open output file.
        self.input = self.parse_input_file(infilename)
        (self.num_phy_regs, self.issue_width) = next(self.input)
        self.out_file = open(outfilename, "w")

        # Various queues or latches connecting different pipeline stages.
        self.decode_queue = pipeline_stage(self.issue_width)
        self.rename_queue = pipeline_stage(self.issue_width)
        self.dispatch_queue = pipeline_stage(self.issue_width)
        self.issue_queue = []
        self.reorder_buffer = []
        self.lsq = load_store_queue()
        self.executing_queue = []

        # Structures to track registers.
        self.map_table = reg_map(ARCH_REGS_COUNT)
        self.free_list = free_list(self.num_phy_regs)
        self.ready_table = ready_queue(self.num_phy_regs)
        self.freeing_registers = []

        # Initially map R0->P0, R1->P1 and so on.
        for register in range(ARCH_REGS_COUNT):
            self.map_table.put(register, self.free_list.get_free_reg())

        # Instructions under consideraion so far.
        self.instructions = []

        # Start from cycle 0.
        self.cycle = 0

        # Track if we are currently fetching an instruction.
        # Used to detect when we have finished scheduling all instructions.
        self.fetching = True

        # Did any stage in the pipeline progress in last cycle?
        # Used to detect if pipeline is stuck because of bad scheduler design.
        self.has_progressed = True


    #
    # Main scheduler functions
    #
    #
    def schedule (self):

        self.fetching = True
        self.has_progressed = True

        while self.is_scheduling() and self.has_progressed:
            
            logging.info("Scheduling: %s" % self)
            
            self.has_progressed = False

            # We process pipeline stages in opposite order to (try to) clear up
            # the subsequent stage before sending instruction forward from any
            # given stage.
            self.commit()
            self.writeback()
            self.issue()
            self.dispatch()
            self.rename()
            self.decode()
            self.fetch()

            # Move on to the next cycle.
            self.advance_cycle()


    def advance_cycle (self):
        for free_reg in self.freeing_registers:
            self.free_list.free(free_reg)
        self.freeing_registers = []

        self.cycle += 1

        logging.debug("Advanced scheduler to cycle # %d" % self.cycle)


    def made_progress (self):
        self.has_progressed = True


    def is_scheduling (self):
        return (
            self.fetching
            or any(not inst.has_commited() for inst in self.instructions)
        )


    #
    # Pipeline stages start here
    # #################################
    #


    #
    # Fetch Stage
    #
    def fetch_inst (self):
        try:
            return next(self.input)
        except StopIteration:
            self.fetching = False
            return None

    def fetch (self):
        fetched = 0
        while self.fetching and fetched < self.issue_width:
            inst = self.fetch_inst()
            if inst is not None:
                inst.fetch_cycle = self.cycle
                self.instructions.append(inst)
                self.decode_queue.pushQ(inst)

                fetched += 1

                self.made_progress()
                logging.debug("Fetched: %s" % inst)


    #
    # Decode Stage
    #
    def decode (self):
        while not self.decode_queue.is_empty():
            inst = self.decode_queue.popQ()
            inst.decode_cycle = self.cycle
            self.rename_queue.pushQ(inst)

            self.made_progress()
            logging.debug("Decoded: %s" % inst)


    #
    # Rename Stage
    #
    def rename (self):
        while not self.rename_queue.is_empty():
	    inst = self.rename_queue.popQ()
            if not self.free_list.is_free() and not inst.is_store_inst():
	  	self.rename_queue.insertQ(inst)
		break

	    inst.src_reg_0 = self.map_table.get(inst.src_reg_0)
	    if inst.src_reg_1 is not None:
		inst.src_reg_1 = self.map_table.get(inst.src_reg_1)
                
            if inst.dst_reg is not None:
                phy_reg = self.free_list.get_free_reg()
                self.map_table.put(inst.dst_reg, phy_reg)
		inst.dst_reg = phy_reg
                self.ready_table.clear(phy_reg)
		
            self.dispatch_queue.pushQ(inst)
            
	    inst.rename_cycle = self.cycle    
            self.made_progress()
            logging.debug("Renamed: %s" % inst)


    #
    # Dispatch Stage
    #
    def dispatch (self):
        while not self.dispatch_queue.is_empty():
            inst = self.dispatch_queue.popQ()
            
            self.issue_queue.append(inst)
            self.reorder_buffer.append(inst)
            
            if inst.is_load_store_inst():
                self.lsq.append(inst)

	    inst.dispatch_cycle = self.cycle		
	    self.made_progress()
            logging.debug("Dispatched: %s" % inst)
	   

    #
    # Issue Stage
    #
    def issue (self):
        issued = 0
        for inst in list(self.issue_queue):
            if issued > self.issue_width:
                break
	    
	    if inst.issue_cycle != None:
	        continue
	    
	    ready = None

	    if self.is_inst_ready(inst):
		ready = 1
	    
            if ready is not None:
                self.executing_queue.append(inst)
		inst.issue_cycle = self.cycle
                self.made_progress()
                logging.debug("Issued: %s" % inst)
                issued += 1


    #
    # Writeback Stage
    #
    def writeback (self):
        for inst in list(self.executing_queue):
	    if inst.issue_cycle == None or inst.writeback_cycle != None or inst.is_load_store_inst():
		continue
	    if inst.dst_reg is not None:
                self.ready_table.ready(inst.dst_reg)
	    inst.writeback_cycle = self.cycle
            self.made_progress()
            logging.debug("Writeback: %s" % inst)
            
        insts = self.lsq.get_executable()
        for inst in list(insts):
	    if inst.issue_cycle == None or inst.writeback_cycle != None:
		continue
            if inst.dst_reg is not None:
                self.ready_table.ready(inst.dst_reg)
	    self.lsq.remove(inst)
	    inst.writeback_cycle = self.cycle
            self.made_progress()
            logging.debug("Writeback Load/Store: %s" % inst)


    #
    # Commit Stage
    #
    def commit (self):
        for inst in list(self.reorder_buffer):
	    if inst.commit_cycle != None:
		continue

            if inst.writeback_cycle == None:
	        break

	    self.freeing_registers.append(inst.dst_reg)
            self.made_progress()
            logging.debug("Committed: %s" % inst)
	    inst.commit_cycle = self.cycle


    def is_inst_ready (self, inst):

        if (not self.ready_table.is_ready(inst.src_reg_0)):
            return False
        
        if (inst.src_reg_1 is not None) and (not self.ready_table.is_ready(inst.src_reg_1)):
            return False

        if inst.is_load_store_inst():
            return self.lsq.can_execute(inst)

        return True




    #
    # File I/O functions
    # #################################
    #

    # Parse input file.
    def parse_input_file (self, infilename):

        # Constant for this project.
        PHY_REG_COUNT_MIN = 32

        try:
            with open(infilename, 'r') as file:
                
                # Regex strings to read field out of file line strings.
                config_parser = re.compile("^(\\d+),(\\d+)$")
                inst_parser = re.compile("^([RILS]),(\\d+),(\\d+),(\\d+)$")

                # Try to parse header
                header = file.readline()
                configs = config_parser.match(header)
                if configs:
                    (num_phy_reg, issue_width) = configs.group(1, 2)
                    num_phy_reg = int(num_phy_reg)
                    issue_width = int(issue_width)

                    if num_phy_reg < PHY_REG_COUNT_MIN:
                        print("Error: Invalid input file header: Number of "
                                "physical register is less than allowed minimum of %d" % (PHY_REG_COUNT_MIN))
                        sys.exit(1)

                    yield (num_phy_reg, issue_width)
                else:
                    print("Error: Invalid input file header!")
                    sys.exit(1)

                # Parse all remaining lines one by one.
                for (index, line) in enumerate(file):
                    configs = inst_parser.match(line)
                    if configs:
                        (Insts, op0, op1, op2) = configs.group(1, 2, 3, 4)

                        yield instruction(index, Insts, int(op0), int(op1), int(op2))
                    else:
                        print("Error: Invalid inst_set: %s" % (line))
                        sys.exit(1)

        except IOError:
            print("Error parsing input file!")
            sys.exit(1)


    def generate_output_file (self):
        if self.is_scheduling():
            self.out_file.write("")
            self.out_file.close()
            return

        for inst in self.instructions:
            self.out_file.write("%s,%s,%s,%s,%s,%s,%s\n" % (
                inst.fetch_cycle,
                inst.decode_cycle,
                inst.rename_cycle,
                inst.dispatch_cycle,
                inst.issue_cycle,
                inst.writeback_cycle,
                inst.commit_cycle,
            ))

        self.out_file.close()

    def __str__ (self):
        return "[out_of_order_scheduler cycle=%d]" % (self.cycle)
        
