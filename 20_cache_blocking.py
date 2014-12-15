import scipy.sparse
import scipy.io
import random
import itertools
import time
from collections import Counter
from scipy.spatial import cKDTree as KDTree
import numpy as np
from twilio.rest import TwilioRestClient

# num bytes in cache line
CACHE_LINE = 64
# num bytes in entry of A,x,y (int = 4 bytes)
DATA_SIZE = 4
# need to store 3 extra ints for a block
BLOCK_OVERHEAD = 3
# num entries of A,x,y that fit in one cache line 
CACHE_LINE_SIZE = CACHE_LINE/DATA_SIZE 
# convert block id (i.e, 1 -> 2x2, to number of bytes to store block
BLOCK_ID_TO_SIZE = {1: BLOCK_OVERHEAD + 1, 
			  2: BLOCK_OVERHEAD + 4, 
			  3: BLOCK_OVERHEAD + 9,
			  4: BLOCK_OVERHEAD + 16}

PAGE_SIZE = 80
# we want one cache block to be within a page, which will also force them to be small enough to remain in cache
CACHE_BLOCK_COLS = PAGE_SIZE/DATA_SIZE
CACHE_BLOCK_ROWS = PAGE_SIZE/DATA_SIZE

#constants
sampling_rate = 0.2
quadrant_sampling_rate = 0.2
height = 7
width = 7
quadrant_threshold = 0.5

################################################################################
####### Blocking component
################################################################################

#runs a sample over the given row_start to row_end and col_start to col_end
#ignores middle_row and middle_col
def run_sample(locations, blocked, sampling_rate, row_start, row_end, col_start, col_end, middle_row, middle_col):
	checked = {}

	results = []
	range_size = (row_end-row_start+1)*(col_end-col_start+1)

	num_samples = int(range_size*sampling_rate)
	num_nonzeros = 0.0
	
	for i in random.sample(xrange(range_size), num_samples):	
		row = i / (row_end-row_start+1) + row_start
		col = i % (row_end-row_start+1) + col_start

		#adjusts row and col if we meant to ignore them
		if row == middle_row and col == middle_col:
			col += 1

		checked[(row,col)] = 1

		#checks if this location is nonzero and not already in a block
		is_nonzero = 0
		if (row,col) in locations and (row,col) not in blocked:
			is_nonzero = 1
			num_nonzeros += 1

		results.append(((row,col),is_nonzero))

	return results, num_nonzeros / num_samples

#after running a sample, counts the number of nonzeroes by quadrant
def count_by_quadrant(results, row_start, row_end, col_start, col_end, middle_row, middle_col):
	quadrant_nonzeros = [0.0,0.0,0.0,0.0]
	quadrant_counts = [0,0,0,0]

	#calculates bounds for the quadrant boxes
	#quadrants are fuzzy, meaning that they extend
	#over each other by 1
	top_bound = row_start + height / 2 + 1
	bottom_bound = row_start + height / 2 -1
	left_bound = col_start + width / 2 + 1
	right_bound = col_start + width / 2 - 1

	#calculates the quadrant for each result
	for ((row,col),is_nonzero) in results:		
		#if we're in quadrant 1
		if row < top_bound and col > right_bound:
			quadrant_counts[0] += 1

			if is_nonzero:
				quadrant_nonzeros[0] += 1

		#if we're in quadrant 2
		if row < top_bound and col < left_bound:
			quadrant_counts[1] += 1

			if is_nonzero:
				quadrant_nonzeros[1] += 1

		#if we're in quadrant 3
		if row > bottom_bound and col < left_bound:
			quadrant_counts[2] += 1

			if is_nonzero:
				quadrant_nonzeros[2] += 1

		#if we're in quadrant 4
		if row > bottom_bound and col > right_bound:
			quadrant_counts[3] += 1

			if is_nonzero:
				quadrant_nonzeros[3] += 1

	return [a/b if b != 0 else 0 for (a,b) in zip(quadrant_nonzeros, quadrant_counts)]

#marks a set of indices (inclusive) as having been blocked
def mark_as_blocked(blocked, cur_row_start, cur_row_end, cur_col_start, cur_col_end):
	for i_ind in range(cur_row_start, cur_row_end+1):
		for j_ind in range(cur_col_start, cur_col_end+1):
			blocked[(i_ind, j_ind)] = 1

#creates a block around a nonzero at i,j
def create_block(i,j, locations, blocked, sampling_rate):
	row_start = i-3
	row_end = i+3
	col_start = j-3
	col_end = j+3	

	#sample the four quadrants
	samples, density = run_sample(locations, blocked, sampling_rate, row_start, row_end, col_start, col_end, i, j)
	quadrants = count_by_quadrant(samples, row_start, row_end, col_start, col_end, i, j)

	#creates a list of quadrant ID and density for that quadrant, sorted by density (descending)
	quadrant_tuples = [(x, quadrants[x]) for x in range(4)]
	quadrant_tuples.sort(key=lambda x: x[1],reverse=True)

	#if any quadrant is dense enough to sample further
	for (quadrant, quadrant_density) in quadrant_tuples:
		if quadrant_density > quadrant_threshold:
			#sets the bounds for this quadrant
			if quadrant == 0:
				cur_row_start = row_start
				cur_row_end = i
				cur_col_start = j
				cur_col_end = col_end					
			elif quadrant == 1:
				cur_row_start = row_start
				cur_row_end = i
				cur_col_start = col_start
				cur_col_end = j
			elif quadrant == 2:
				cur_row_start = i
				cur_row_end = row_end
				cur_col_start = col_start
				cur_col_end = j
			else:
				cur_row_start = i
				cur_row_end = row_end
				cur_col_start = j
				cur_col_end = col_end

			#runs a deeper sample on this quadrant
			deeper_samples, deeper_density = run_sample(locations, blocked, sampling_rate, cur_row_start, cur_row_end, cur_col_start, cur_col_end, i, j)					

			#updates our density with this new information
			quadrants[quadrant] = deeper_density

			#checks if this new density is above the threshold
			if deeper_density > quadrant_threshold:				
				#returns these bounds
				return cur_row_start, cur_row_end, cur_col_start, cur_col_end

	#otherwise, since no quadrants were dense enough, search the ring of 8 around (i,j)
	ring = [(i-1,j-1),(i-1,j),(i-1,j+1),(i,j-1),(i,j+1),(i+1,j-1),(i+1,j),(i+1,j+1)]
	ring_nonzeros = 0
	ring_nonzeros_list = []

	for (ri,rj) in ring:
		#counts nonzeros in this ring
		if (ri,rj) in locations and (ri, rj) not in blocked:
			ring_nonzeros += 1
			ring_nonzeros_list.append((ri,rj))

		#immediately exit if we have 3 or more nonzeros
		if ring_nonzeros > 2:						
			#returns this range
			return i-1, i+1, j-1, j+1	

	#after looping through all surrounding points, if we have 3 or more nonzeros
	if ring_nonzeros > 2:			
		#returns this range
		return i-1, i+1, j-1, j+1

	#otherwise, use the range method to select a box around (i,j)
	else:
		if len(ring_nonzeros_list) == 0:
			cur_row_start = i
			cur_row_end = i
			cur_col_start = j
			cur_col_end = j
		else:
			cur_row_start = min(i,min(x[0] for x in ring_nonzeros_list))
			cur_row_end = max(i,max(x[0] for x in ring_nonzeros_list))
			cur_col_start = min(j,min(x[1] for x in ring_nonzeros_list))
			cur_col_end = max(j,max(x[1] for x in ring_nonzeros_list))

		cur_block_height = cur_row_end-cur_row_start+1
		cur_block_width = cur_col_end-cur_col_start+1	

		#in the case where we have a 2x3 box, then
		#return the 3x3 box around (i,j)
		#h = (cur_row_end-cur_row_start+1) = 2 or 3
		#w = (cur_col_start-cur_col_end+1) = 3 or 2 respectively,
		#so h+w = 5, and
		# cur_row_end-cur_row_start+cur_col_start-cur_col_end=3
		if (cur_block_height)+(cur_block_width)==5:
			#returns these boundss
			return i-1, i+1, j-1, j+1

		#in the case where we have a 1x2 box, then
		#return the 2x2 box around (i,j) by expanding either direction
		#h = (cur_row_end-cur_row_start+1) = 1 or 2
		#w = (cur_col_start-cur_col_end+1) = 2 or 1 respectively,
		#so h+w = 3, and
		# cur_row_end-cur_row_start+cur_col_start-cur_col_end=1
		elif (cur_block_height)+(cur_block_width)==3:
			if cur_block_height == 1:
				cur_row_end += 1
			#otherwise, cur_block_width must == 1
			else:
				cur_col_end += 1

			#returns these bounds
			return cur_row_start, cur_row_end, cur_col_start, cur_col_end			

		#otherwise, just return the box we've just found		
		return cur_row_start, cur_row_end, cur_col_start, cur_col_end

#given a block, checks if it is within the bounds of the matrix
#if not, clips it to a new square block that does fit
def clip_block(cur_row_start, cur_row_end, cur_col_start, cur_col_end, matrix_rows, matrix_cols, coord_to_block):
	#if we are off the bottom edge
	if cur_row_end > matrix_rows-1:
		offset = (cur_row_end - (matrix_rows-1))
		cur_row_end -= offset
		cur_row_start -= offset
	#if we are above the top edge
	elif cur_row_start < 0:
		offset = (cur_row_start)
		cur_row_start -= offset
		cur_row_end -= offset

	#if we are off the right edge
	if cur_col_end > matrix_cols-1:
		offset = (cur_col_end - (matrix_cols-1))
		cur_col_end -= offset
		cur_col_start -= offset

	#if we are off the left edge
	elif cur_col_start < 0:
		offset = (cur_col_start)
		cur_col_start -= offset
		cur_col_end -= offset

	# is our top left already covered?
	can_shrink = True
	while ((cur_row_start, cur_col_start) in coord_to_block and can_shrink):
		cur_row_start += 1
		cur_col_start += 1
		if (cur_row_end - cur_row_start) == 1:
			can_shrink = False
		if (cur_col_end - cur_col_start) == 1:
			can_shrink = False
	
	if (cur_row_start, cur_col_start) in coord_to_block:
		raise Exception("Trying to create a block whose upper left coord already exists") 

	return cur_row_start, cur_row_end, cur_col_start, cur_col_end
		

#runs blocking over a matrix in COO format
def run_blocking(cx):
	#a dictionary of (row, col) tuples to store nonzeros
	locations = {}
	blocked = {}	

	#a list of generated blocks
	blocks = []

	#a dictionary mapping of coordinates to blocks
	coord_to_block = {}

	#dictionary mapping cache blocks to list of coordinates in that cache block
	cache_block_to_coords = {}

	#creates a dictionary of nonzeros marking tuples of (row, col) as nonzero
	for i,j,v in itertools.izip(cx.row, cx.col, cx.data):
	    locations[(i,j)] = v		

	our_area = 0

	matrix_height = cx.shape[0]
	matrix_width = cx.shape[1]

	#iterates through the nonzeros, creating blocks
	for i,j,v in itertools.izip(cx.row, cx.col, cx.data):		

		#if this location hasn't been blocked
		if (i,j) not in blocked:
			#create a new block around this area
			cur_row_start, cur_row_end, cur_col_start, cur_col_end = create_block(i, j, locations, blocked, sampling_rate)
			
			cur_row_start, cur_row_end, cur_col_start, cur_col_end = clip_block(cur_row_start, cur_row_end, cur_col_start, cur_col_end, matrix_height, matrix_width, coord_to_block)

			#determines the size of this block 
			#(we only have square blocks for now, so we only need one dimension)			
			cur_block_height = cur_row_end - cur_row_start + 1			
			cur_block_width = cur_col_end - cur_col_start + 1		

			#format for blocks: 
			#size, row_start, col_start, unpacked values
			cur_block_list = [0 for i in range(cur_block_width*cur_block_height+3)]
			cur_block_list[0] = cur_block_height
			cur_block_list[1] = cur_row_start
			cur_block_list[2] = cur_col_start

			for count in range(cur_block_width * cur_block_height):
				x = count / cur_block_height + cur_row_start
				y = count % cur_block_height + cur_col_start

				#unpacks the nonzero elements into the block
				if (x,y) in locations and (x,y) not in blocked:					
					cur_block_list[count+3] = locations[(x,y)]

			#blocks.append(cur_block_list)

			#stores this block by coordinate
			coord_to_block[(cur_row_start, cur_col_start)] = cur_block_list

			#calculate cache block 'region' which we're in
			cache_block_row = cur_row_start / CACHE_BLOCK_ROWS
			cache_block_col = cur_col_start / CACHE_BLOCK_COLS

			if (cache_block_row, cache_block_col) not in cache_block_to_coords:
				cache_block_to_coords[(cache_block_row, cache_block_col)] = [0, 0, []]
			
			#updates the size within this cache block
			cache_block_to_coords[(cache_block_row, cache_block_col)][0] += BLOCK_ID_TO_SIZE[cur_block_height]

			#updates the count of blocks within this cache block
			cache_block_to_coords[(cache_block_row, cache_block_col)][1] += 1

			#appends the current (row, col) to the list of coordinates
			cache_block_to_coords[(cache_block_row, cache_block_col)][2].append((cur_row_start, cur_col_start))

			#increment our total area by this amount
			#our_area += (cur_row_end - cur_row_start + 1) * (cur_col_end - cur_col_start + 1)		

			#mark this current block as blocked
			mark_as_blocked(blocked, cur_row_start, cur_row_end, cur_col_start, cur_col_end)	

			#print cur_row_start, cur_row_end, cur_col_start, cur_col_end

	#simulates the naive blocking area
	#naive_w = 4
	#naive_h = 4
	#naive_area = 0
	#for i in range(cx.shape[0]/naive_h+1):
	#	for j in range(cx.shape[1]/naive_w+1):
	#		naive_count = 0
	#		for x in range(naive_h):
	#			for y in range(naive_w):
	#				if (i*naive_h+x,j*naive_w+y) in locations:
	#					naive_count += 1
	#		if naive_count != 0:
	#			naive_area += naive_h * naive_w

	#print "Length:", len(cx.row)
	#print "Our area:", our_area
	#print "Naive ",naive_w,"by",naive_h,"area:", naive_area
	#print "Compression", float(naive_area)/our_area

	return coord_to_block, cache_block_to_coords, blocked, locations

################################################################################
####### Superblock arrangement component
################################################################################

def to_tup(np_array):
	"""
	turn a numpy array to a tuple
	"""
	if not isinstance(np_array, tuple):
		return tuple(list(np_array))
	else:
		return np_array

def construct_superblock(block_coords):
	"""
	For any point, we can query the closest, under np.inf norm (max_distance = cache_line)
	if we can't find any, find any block in 'close' row that hasn't been blocked
	find any block in '' col that hasn't been blocked
 	"""
	if len(block_coords) < 1:
		return []
	
	# list that contains the blocks that we haven't added	
	#remaining_blocks = set([tuple(coord) for coord in list(block_coords)]) 
	remaining_blocks = set(block_coords)
	block_coords = np.array(block_coords)
	super_block = []
	coords_in_super_block = {}
	num_blocks = block_coords.shape[0]

	# block_coords: list of block upper left coords in the current cache-block of A
	range_tree = KDTree(data = block_coords)
	num_points_in_rtree = range_tree.data.shape[0]

	# lexical sort by column then by row
	row_first = block_coords[np.lexsort((block_coords[:, 1], block_coords[:, 0]))]
	col_first = block_coords[np.lexsort((block_coords[:, 0], block_coords[:, 1]))]
	
	# add the first block
	cur_block = row_first[0]
	cur_block_tup = to_tup(cur_block)

	super_block.append(cur_block_tup)
	coords_in_super_block[cur_block_tup] = True
	remaining_blocks.remove(cur_block_tup)
	blocks_added = 1
	
	while (blocks_added < num_blocks ):		

		found_next_block = False
		deleted = False
		_, ind_closest_block_coords = range_tree.query(cur_block, 
													k=20, 
													p=np.inf, 
													distance_upper_bound = CACHE_LINE_SIZE)

		for ind in ind_closest_block_coords:
			# iterate through the indices, make sure they are valid
			if ind >= num_points_in_rtree:
				continue
			cand_coord = to_tup(range_tree.data[ind])
			if cand_coord not in coords_in_super_block:
				found_next_block = True
				next_block = cand_coord
				
		if not found_next_block:	
			# move in both directions in both lists
			# find where we are in the sorted row list 
			r_ind = np.searchsorted(row_first[:, 0], cur_block[0])
			# find where we are in the sorted col list
			c_ind = np.searchsorted(col_first[:, 1], cur_block[1])
			i = 1
			some_block_in_range = False
			while some_block_in_range and (not found_next_block):
				some_block_in_range = False
				# top row
				if abs(row_first[r_ind + i, 0] - cur_block[0]) <= CACHE_LINE_SIZE: 
					some_block_in_range = True
					top_row_block_tup = to_tup(row_first[r_ind + i, 0])
					if top_row_block_tup not in coords_in_super_block:
						found_next_block = True
						next_block = top_row_block_tup
				# bottom row
				if abs(row_first[r_ind - i, 0] - cur_block[0]) <= CACHE_LINE_SIZE: 
					some_block_in_range = True
					bot_row_block_tup = to_tup(row_first[r_ind - i, 0])
					if bot_row_block_tup not in coords_in_super_block:
						found_next_block = True
						next_block = bot_row_block_tup
				# top column 
				if abs(col_first[c_ind + i, 0] - cur_block[0]) <= CACHE_LINE_SIZE: 
					some_block_in_range = True
					top_col_block_tup = to_tup(col_first[c_ind + i, 0])
					if top_col_block_tup not in coords_in_super_block:
						found_next_block = True
						next_block = top_col_block_tup
				# bottom column
				if abs(col_first[c_ind - i, 0] - cur_block[0]) <= CACHE_LINE_SIZE: 
					some_block_in_range = True
					boht_col_block_tup = to_tup(col_first[c_ind - i, 0])
					if bot_col_block_tup not in coords_in_super_block:
						found_next_block = True
						next_block = bot_col_block_tup					
				i += 1
					
		if not found_next_block:
			# still haven't found it, want to go through remaining blocks and add any one that hasn't been used
			while not found_next_block:
				#print blocks_added, num_blocks, len(remaining_blocks)
				cand_block = remaining_blocks.pop()
				if cand_block not in coords_in_super_block:
					found_next_block = True
					next_block = cand_block
					deleted = True
		
		if not found_next_block:
			raise Exception("Error, did not find block after brute force!")
		
		# next_block can be a tuple or a block at this time, but we want a tuple
		cur_block = np.array(next_block)
		next_block_tup = to_tup(next_block)
		super_block.append(next_block_tup)
		coords_in_super_block[next_block_tup] = True
		
		if not deleted:
			remaining_blocks.remove(next_block_tup)
		
		blocks_added += 1
	return super_block

def write_superblocks(superblocks, coord_to_block, fname, cx):
	"""
	assumes: 
		superblock = (size, num_blocks, blocks)
		val: unrolled elements of block (row major order)
	"""
	total_num_blocks = 0
	total_area_blocks = 0
	total_nonzero_el = 0

	outfile = open(fname, 'w')
	num_sblocks = len(superblocks)
	outfile.write("%d %d %d\n" % (cx.shape[0], cx.shape[1], num_sblocks))
	for sblock in superblocks:
		size, num_blocks, blocks = sblock  
		outfile.write('%d %d ' % (size, num_blocks))
		for block_coord in blocks:
			total_num_blocks += 1

			whole_block = coord_to_block[block_coord]
			block_id, row, col = whole_block[:3]
			vals = whole_block[3:]
			outfile.write("%d %d %d " % (block_id, row, col))
			for val in vals:
				total_area_blocks += 1
				outfile.write("%d " % val)
				if val != 0:
					total_nonzero_el += 1

		outfile.write("\n")
	outfile.close()

	return total_num_blocks, total_area_blocks, total_nonzero_el

def naive_blocking(cx, locations, block_row_size, block_col_size, filename):
	#row, col, list of vals

	outfile = open(filename+'_naive_output.txt', 'w')

	#for each superblock region:
	#	number_blocks * 18 + 1
	#print size, num blocks, blocks

	#loops through 'superblock' regions
	'''
	for i in range(cx.shape[0]/region_size+1):
		for j in range(cx.shape[1]/region_size+1):
			has_changed_in_super = False
			blocks_in_cur_superblock = []
			#within this 'superblock' region, loop through 4x4 blocks
			for k in range(region_size/block_size):
				for l in range(region_size/block_size):
					cur_block_row = i*region_size+k*block_size
					cur_block_col = j*region_size+l*block_size

					cur_vals = [cur_block_row,cur_block_col,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0,0]
					has_changed = False
					#within this 4x4 block, loop through the elements
					for m in range(4):
						for n in range(4):
							cur_coord = (cur_block_row+m,cur_block_col+n)									
							if cur_coord in locations:
								cur_vals[2+m*4+n] = locations[cur_coord]
								has_changed = True
								has_changed_in_super = True

					if has_changed:						
						blocks_in_cur_superblock.append(cur_vals)	

			blocks.append(blocks_in_cur_superblock)
	'''

	#loops through nonzeros in row-major order
	
	#constructs coords of nonzeros
	row_indices = cx.row
	col_indices = cx.col
	coords = np.transpose(np.vstack((np.array(row_indices), np.array(col_indices))))

	# lexical sort coords by row and then column
	row_first = coords[np.lexsort((coords[:, 1], coords[:, 0]))]

	regions = {}
	blocks = {}

	blocks_created = 0

	#for each nonzero:
	#	calc region row and col
	#	if this region is not in the dict, add it as []
	#	calculate block row and column
	#	if this block is not in blocks:
	#		add this block coords to blocks
	#		add this block to regions[(region_row, region_col)]

	#loop through nonzeros
	for (r,c) in coords:
		#snaps to nearest block
		block_row = r/block_row_size * block_row_size
		block_col = c/block_col_size * block_col_size

		#only do this is (r,c) hasn't yet been blocked		
		if (block_row, block_col) not in blocks:			
			region_row = r/CACHE_BLOCK_ROWS
			region_col = c/CACHE_BLOCK_COLS

			cur_vals = [0 for i in range(block_row_size*block_col_size+2)]

			cur_vals[0] = block_row
			cur_vals[1] = block_col

			have_nonzero = False

			#loops through poins in this region
			for r1 in range(0,block_row_size):
				for c1 in range(0, block_col_size):
					cur_block_row = block_row + r1
					cur_block_col = block_col + c1
					
					#if this is a nonzero element as well
					if (cur_block_row, cur_block_col) in locations:
						cur_vals[2+r1*block_col_size+c1] = locations[(cur_block_row, cur_block_col)]
						have_nonzero = True

			if have_nonzero:
				blocks[(block_row, block_col)] = 1

				if (region_row, region_col) not in regions:
					regions[(region_row, region_col)] = []

				#if we have nonzero elements, add it to the list for this region
				regions[(region_row, region_col)].append(cur_vals)

				blocks_created += 1
	

	#prints the number of rows, number of columns, and number of superblocks to file
	outfile.write("%d %d %d\n" % (cx.shape[0], cx.shape[1], len(regions.keys())))	

	#prints out the regions that we've found
	for region_row in range(0, cx.shape[0]/CACHE_BLOCK_ROWS+1):
		for region_col in range(0, cx.shape[1]/CACHE_BLOCK_COLS+1):

			#if we have data for this region
			if (region_row, region_col) in regions:
				cur_blocks = regions[(region_row, region_col)]

				size = len(cur_blocks)
				cost = size * len(cur_blocks[0]) + 1

				#writes the cost and number of blocks
				outfile.write("%d %d " % (cost, size))

				#writes the nonzero elements
				for block in cur_blocks:
					for i in block:
						outfile.write("%d " % i)
				
				outfile.write("\n")

	outfile.close()

	return blocks_created

							


#reads in one of several available matrices
#cx = scipy.io.mmread("ch5-5-b3.mtx")
#cx = scipy.io.mmread("tub1000.mtx")
#cx = scipy.io.mmread("145bit.mtx")
#cx = scipy.io.mmread("sherman1.mtx")
#cx = scipy.io.mmread("saylr3.mtx")
#cx = scipy.io.mmread("dwt_1005.mtx")
#cx = scipy.io.mmread("cryg10000.mtx")
#cx = scipy.io.mmread("bloweybq.mtx")

#files = ["ch5-5-b3.mtx", "tub1000.mtx", "145bit.mtx", "sherman1.mtx", "saylr3.mtx", "dwt_1005.mtx", "cryg10000.mtx", "bloweybq.mtx", "qpband.mtx"]
#files =["Tina_AskCal.mtx"]
#files =["ch5-5-b3.mtx"]
#files =["saylr3.mtx"]
#files =["geom.mtx"]

#files =["me2010.mtx"]

#files = ['lp_pds_10']
#files = [('test_matrices/Tina_AskCal.mtx','test_matrices/Tina_AskCal'),]
files = [
#	('test_matrices/Tina_AskCal.mtx','test_matrices/Tina_AskCal'),
	('20_block_test_matrices/rail516.mtx','20_block_test_matrices/rail516'),
#	('test_matrices/lp_pds_10.mtx','test_matrices/lp_pds_10'),
#	('test_matrices/ch7-9-b5.mtx','test_matrices/ch7-9-b5'),
#	('test_matrices/nh2010.mtx','test_matrices/nh2010'),	
#	('test_matrices/TF17.mtx','test_matrices/TF17'),
#	('test_matrices/wy2010.mtx','test_matrices/wy2010'),
	('20_block_test_matrices/shar_te2-b3.mtx', '20_block_test_matrices/shar_te2-b3'),
#	('test_matrices/il2010.mtx', 'test_matrices/il2010'),
#	('test_matrices/mc2depi.mtx', 'test_matrices/mc2depi'),
#	('test_matrices/ut2010.mtx', 'test_matrices/ut2010'),
	('20_block_test_matrices/lp_pds_02.mtx', '20_block_test_matrices/lp_pds_02'),
#	('test_matrices/roadNet-PA.mtx', 'test_matrices/roadNet-PA'),
]

for filename, filelabel in files:			
	cx = scipy.io.mmread(filename)
	rand_width = 100000
	rand_height = 100000
	#cx = scipy.sparse.rand(rand_height, rand_width, density=0.00001,dtype=np.dtype('bool')).tocoo()	
	

	#cx = scipy.sparse.rand(rand_height, rand_width, density=0.001, dtype=np.dtype('bool')).tocoo()
	#cy = scipy.sparse.rand(rand_height, rand_width, density=0.001, dtype=np.dtype('bool')).tocoo()

	#A = cx.todense()
	#B = cy.todense()
	#cx  = scipy.sparse.coo_matrix(A - B)

	
	matrix_rows = cx.shape[0]
	matrix_cols = cx.shape[1]

	#writes out to the vector of all 1s
	vector_of_1s = open(filelabel + '_vector.txt', 'w')
	vector_of_1s.write("%d " % matrix_cols)
	for i in range(matrix_cols):
		vector_of_1s.write("1 ")
	vector_of_1s.close()

	start_time = time.time()

	#creates the blocking
	coord_to_block, cache_block_to_coords, blocked, locations = run_blocking(cx)
	
	finished_blocking = time.time()

	print "Finished blocking"

	#checks again for blocks that were never blocked	
	for i,j,v in itertools.izip(cx.row, cx.col, cx.data):		
		if (i,j) not in blocked:
			cur_block_list = [1,i,j,v]

			#stores this block by coordinate
			if (i,j) in coord_to_block:
				print "Error!!!!!"

			coord_to_block[(i, j)] = cur_block_list

			#calculate cache block 'region' which we're in
			cache_block_row = i / CACHE_BLOCK_ROWS
			cache_block_col = j / CACHE_BLOCK_COLS

			if (cache_block_row, cache_block_col) not in cache_block_to_coords:
				cache_block_to_coords[(cache_block_row, cache_block_col)] = [0, 0, []]
			
			#updates the size within this cache block
			cache_block_to_coords[(cache_block_row, cache_block_col)][0] += BLOCK_ID_TO_SIZE[1]

			#updates the count of blocks within this cache block
			cache_block_to_coords[(cache_block_row, cache_block_col)][1] += 1

			#appends the current (row, col) to the list of coordinates
			cache_block_to_coords[(cache_block_row, cache_block_col)][2].append((i, j))

			#increment our total area by this amount
			#our_area += (cur_row_end - cur_row_start + 1) * (cur_col_end - cur_col_start + 1)		

			#mark this current block as blocked
			mark_as_blocked(blocked, i, i, j, j)	

	print "Finished reblocking"

	#arranges blocks into superblocks
	superblocks = []
	for i in range(matrix_rows/CACHE_BLOCK_ROWS + 1): 
		for j in range(matrix_cols/CACHE_BLOCK_COLS + 1):
			if (i, j) in cache_block_to_coords:
				# get the superblock
				size, num_blocks, unordered_sblock = cache_block_to_coords[(i, j)]
				ordered_sblock = construct_superblock(unordered_sblock)				
				# reorder the superblock
				superblocks.append((size, num_blocks, ordered_sblock))
	
	num_our_blocks, area_our_blocks, nonzero_count = write_superblocks(superblocks, coord_to_block, filelabel+"_output.txt", cx)
	end_time = time.time()

	finished_superblocking = time.time()

	print "Finished superblocking"

	#print len(coord_to_block.keys()), "blocks"	
	#print len(cache_block_to_coords.keys()), "cache blocks"

	#10 1 1 1 1 1 1 1 1 1 1
	#50 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1 1
	#print "Expected product: ", cx * [1 for i in range(cx.shape[1])]

	#writes the expected output to a file
	expected_output_file = open(filelabel+"_expected_result.txt", "w")
	for result in cx * [1 for i in range(cx.shape[1])]:
		expected_output_file.write('%d' % result)
		expected_output_file.write(' ')
	expected_output_file.close()

	finished_writing_expected_output = time.time()

	num_naive_blocks = naive_blocking(cx, locations, 4, 4, filelabel)

	finished_naive_blocking = time.time()

	#writes results to file
	outfile = open('time_results.txt', 'a')
	outfile.write('%s %f %f %f %f %d %d %d %d\n' % (filelabel, finished_blocking - start_time, finished_superblocking - finished_blocking, finished_writing_expected_output - finished_superblocking, finished_naive_blocking - finished_writing_expected_output, num_naive_blocks, num_our_blocks, num_naive_blocks*16, area_our_blocks))
	outfile.close()
	

	#prints command to screen
	#print "./run", filelabel+'_output.txt', filelabel+'_vector.txt', filelabel+'_calculated_result.txt'
	#print "diff", filelabel+'_calculated_result.txt', filelabel+'_expected_result.txt'
	#print "./naive", filelabel+'_naive_output.txt', filelabel+'_vector.txt', filelabel+'_naive_result.txt'
	#print "diff", filelabel+'_naive_result.txt', filelabel+'_expected_result.txt'

	#texts to alert
	account_sid = "ACa0741808987f84fd6e5f6c8564dbbff5"
	auth_token  = "432c3adfab7c910e9519bf07e7976b9a"
	client = TwilioRestClient(account_sid, auth_token)

	client.messages.create(to="+14074324062", from_="+18102923084", body=filelabel+": "+str(nonzero_count)+" vs "+str(len(cx.row)))

