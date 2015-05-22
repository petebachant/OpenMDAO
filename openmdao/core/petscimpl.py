
import sys
import numpy

from openmdao.core.vecwrapper import SrcVecWrapper, TgtVecWrapper
from openmdao.core.dataxfer import DataXfer

from petsc4py import PETSc

class PetscImpl(object):
    """PETSc vector and data transfer implementation factory"""

    @staticmethod
    def create_src_vecwrapper(pathname, comm):
        """
        Create a`PetscSrcVecWrapper`

        Returns
        -------
        `PetscSrcVecWrapper`
        """
        return PetscSrcVecWrapper(pathname, comm)

    @staticmethod
    def create_tgt_vecwrapper(pathname, comm):
        """
        Create a `PetscTgtVecWrapper`

        Returns
        -------
        `PetscTgtVecWrapper`
        """
        return PetscTgtVecWrapper(pathname, comm)

    @staticmethod
    def create_data_xfer(varmanager, src_idxs, tgt_idxs, flat_conns, noflat_conns):
        """
        Create an object for performing data transfer between source
        and target vectors

        Parameters
        ----------
        varmanager : `VarManager`
            The `VarManager` that managers this data transfer

        src_idxs : array
            indices of the source variables in the source vector

        tgt_idxs : array
            indices of the target variables in the target vector

        flat_conns : dict
            mapping of flattenable variables to the source variables that
            they are connected to

        noflat_conns : dict
            mapping of non-flattenable variables to the source variables that
            they are connected to

        Returns
        -------
        `PetscDataXfer`
            a `PetscDataXfer` object
        """
        return PetscDataXfer(varmanager, src_idxs, tgt_idxs, flat_conns, noflat_conns)

    @staticmethod
    def create_app_ordering(varmanager):
        """Creates a PETSc application ordering."""
        comm = varmanager.comm
        local_unknown_sizes = varmanager._local_unknown_sizes
        unknowns_vec = varmanager.unknowns
        rank = comm.rank

        start = numpy.sum(local_unknown_sizes[:rank])
        end = numpy.sum(local_unknown_sizes[:rank+1])
        to_idx_array = unknowns_vec.make_idx_array(start, end)

        app_idxs = []
        for ivar, (name, v) in enumerate(unknowns_vec.get_vecvars()):
            start = numpy.sum(local_unknown_sizes[:,    :ivar]) + \
                    numpy.sum(local_unknown_sizes[:rank, ivar])
            end = start + local_unknown_sizes[rank, ivar]
            app_idxs.append(unknowns_vec.make_idx_array(start, end))

        if app_idxs:
            app_idxs = numpy.concatenate(app_idxs)

        app_ind_set = PETSc.IS().createGeneral(app_idxs, comm=comm)
        petsc_ind_set = PETSc.IS().createGeneral(to_idx_array, comm=comm)

        return PETSc.AO().createBasic(app_ind_set, petsc_ind_set, comm=comm)

class PetscSrcVecWrapper(SrcVecWrapper):

    idx_arr_type = PETSc.IntType

    def setup(self, unknowns_dict, store_noflats=False):
        """
        Create internal data storage for variables in unknowns_dict.

        Parameters
        ----------
        unknowns_dict : `OrderedDict`
            A dictionary of absolute variable names keyed to an associated
            metadata dictionary.

        store_noflats : bool
            Indicates of noflat vars should be stored.  This is only true
            for the unknowns vecwrapper.
        """
        super(PetscSrcVecWrapper, self).setup(unknowns_dict, store_noflats=store_noflats)
        self.petsc_vec = PETSc.Vec().createWithArray(self.vec, comm=self.comm)

    def _get_flattened_sizes(self):
        """
        Collect all flattenable var sizes.

        Returns
        -------
        ndarray
            array containing local sizes of flattenable unknown variables for
            every process in our communicator.
        """
        sizes = [m['size'] for m in self.values() if not m.get('noflat')]

        # create 2D array of variable sizes per process
        self.local_unknown_sizes = numpy.zeros((self.comm.size, len(sizes)), int)

        # create a vec indicating whether a nonflat variable is active
        # in this rank or not
        #self.noflat_isactive = numpy.zeros((size, len(self.noflat_vars)), int)

        # create our row in the local_unknown_sizes table
        ours = numpy.zeros((1, len(sizes)), int)
        for i, (name, meta) in enumerate(self.get_vecvars()):
            ours[0, i] = meta['size']

        #our_noflats = numpy.zeros((1, len(self.get_noflats())), int)
        #for i, (name, meta) in enumerate(self.get_noflats()):
            #our_noflats[0, i] = int(self.is_variable_local(name[0]))

        # collect local var sizes from all of the processes in our comm
        # these sizes will be the same in all processes except in cases
        # where a variable belongs to a multiprocessor component.  In that
        # case, the part of the component that runs in a given process will
        # only have a slice of each of the component's variables.
        self.comm.Allgather(ours[0,:], self.local_unknown_sizes)
        #comm.Allgather(our_noflats[0,:], self.noflat_isactive)

        self.local_unknown_sizes[self.comm.rank, :] = ours[0, :]

        return self.local_unknown_sizes

    def get_global_idxs(self, name):
        """
        Get all of the indices for the named variable into the full distributed
        vector.

        Parameters
        ----------
        name : str
            name of variable to get the indices for

        Returns
        -------
        ndarray
            Index array containing all distributed indices for the named variable.
        """
        meta = self._vardict[name]
        if meta.get('noflat'):
            raise RuntimeError("No vector indices can be provided for non-flattenable variable '%s'" % name)

        start, end = self._slices[name]
        return self.make_idx_array(start, end)

    def norm(self):
        """
        Returns
        -------
        float
            The norm of the distributed vector
        """
        self.petsc_vec.assemble()
        return self.petsc_vec.norm()


class PetscTgtVecWrapper(TgtVecWrapper):
    idx_arr_type = PETSc.IntType

    def setup(self, parent_params_vec, params_dict, srcvec, my_params,
              connections, store_noflats=False):
        """
        Configure this vector to store a flattened array of the variables
        in params_dict. Variable shape and value are retrieved from srcvec.

        Parameters
        ----------
        parent_params_vec : `VecWrapper` or None
            `VecWrapper` of parameters from the parent `System`

        params_dict : `OrderedDict`
            Dictionary of parameter absolute name mapped to metadata dict

        srcvec : `VecWrapper`
            Source `VecWrapper` corresponding to the target `VecWrapper` we're building.

        my_params : list of str
            A list of absolute names of parameters that the `VecWrapper` we're building
            will 'own'.

        connections : dict of str : str
            A dict of absolute target names mapped to the absolute name of their
            source variable.

        store_noflats : bool (optional)
            If True, store unflattenable variables in the `VecWrapper` we're building.
        """
        super(PetscTgtVecWrapper, self).setup(parent_params_vec, params_dict,
                                              srcvec, my_params,
                                              connections, store_noflats)
        self.petsc_vec = PETSc.Vec().createWithArray(self.vec, comm=self.comm)

    def _get_flattened_sizes(self):
        """
        Create a 1x1 numpy array to hold the sum of the sizes of local
        flattenable params.

        Returns
        -------
        ndarray
            array containing sum of local sizes of flattenable params.
        """
        psize = sum([m['size'] for m in self.values()
                     if m.get('owned') and not m.get('noflat')])

        return numpy.array(self.comm.allgather(psize), int)


class PetscDataXfer(DataXfer):
    def __init__(self, varmanager, src_idxs, tgt_idxs, flat_conns, noflat_conns):
        """
        Parameters
        ----------
        varmanager : `VarManager`
            The `VarManager` that managers this data transfer

        src_idxs : array
            indices of the source variables in the source vector

        tgt_idxs : array
            indices of the target variables in the target vector

        flat_conns : dict
            mapping of flattenable variables to the source variables that
            they are connected to

        noflat_conns : dict
            mapping of non-flattenable variables to the source variables that
            they are connected to
        """
        super(PetscDataXfer, self).__init__(src_idxs, tgt_idxs,
                                            flat_conns, noflat_conns)

        self.comm = comm = varmanager.comm
        uvec = varmanager.unknowns.petsc_vec
        pvec = varmanager.params.petsc_vec

        src_idx_set = PETSc.IS().createGeneral(src_idxs, comm=comm)
        tgt_idx_set = PETSc.IS().createGeneral(tgt_idxs, comm=comm)

        src_idx_set = varmanager.app_ordering.app2petsc(src_idx_set)

        try:
            self.scatter = PETSc.Scatter().create(uvec, src_idx_set,
                                                  pvec, tgt_idx_set)
        except Exception as err:
            raise RuntimeError("ERROR in %s (src_idxs=%s, tgt_idxs=%s, usize=%d, psize=%d): %s" %
                               (system.name, src_idxs, tgt_idxs,
                                varmanager.unknowns.vec.size,
                                varmanager.params.vec.size, str(err)))

    def transfer(self, srcvec, tgtvec, mode='fwd'):
        """Performs data transfer between a distributed source vector and
        a distributed target vector.

        Parameters
        ----------
        src_idxs : array
            indices of the source variables in the source vector

        tgt_idxs : array
            indices of the target variables in the target vector

        flat_conns : dict
            mapping of flattenable variables to the source variables that
            they are connected to

        noflat_conns : dict
            mapping of non-flattenable variables to the source variables that
            they are connected to

        mode : 'fwd' or 'rev' (optional)
            direction of the data transfer, source to target ('fwd', the default)
            or target to source ('rev').
        """
        if mode == 'rev':
            # in reverse mode, srcvec and tgtvec are switched. Note, we only
            # run in reverse for derivatives, and derivatives accumulate from
            # all targets. This requires numpy's new add command.
            np.add.at(srcvec.vec, self.src_idxs, tgtvec.vec[self.tgt_idxs])

            # formerly
            #srcvec.vec[self.src_idxs] += tgtvec.vec[self.tgt_idxs]

            # noflats are never scattered in reverse, so skip that part

        else:  # forward
            tgtvec.vec[self.tgt_idxs] = srcvec.vec[self.src_idxs]

            for tgt, src in self.noflat_conns:
                tgtvec[tgt] = srcvec[src]