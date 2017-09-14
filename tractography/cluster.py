""" cluster.py

Module containing classes and functions used to cluster fibers and modify
parameters pertaining to clusters.

"""

import numpy as np
import os, scipy.cluster, sklearn.preprocessing
from joblib import Parallel, delayed
from sys import exit

import fibers, distance, misc
import vtk

def spectralClustering(inputVTK, scalarDataList=[], scalarTypeList=[], scalarWeightList=[],
                                    pts_per_fiber=20, k_clusters=10, sigma=0.4, saveAllSimilarity=False,
                                    saveWSimilarity=False, dirpath=None, verbose=0, no_of_jobs=1):
        """
        Clustering of fibers based on pairwise fiber similarity.
        See paper: "A tutorial on spectral clustering" (von Luxburg, 2007)

        If no scalar data provided, clustering performed based on geometry.
        First element of scalarWeightList should be weight placed for geometry, followed by order
        given in scalarTypeList These weights should sum to 1.0 (weights given as a decimal value).
        ex.  scalarDataList
              scalarTypeList = [FA, T1]
              scalarWeightList = [Geometry, FA, T1]

        INPUT:
            inputVTK - input polydata file
            scalarDataList - list containing scalar data for similarity measurements; defaults empty
            scalarTypeList - list containing scalar type for similarity measurements; defaults empty
            scalarWeightList - list containing scalar weights for similarity measurements; defaults empty
            pts_per_fiber - number of samples to take along each fiber
            k_clusters - number of clusters via k-means clustering; defaults 10 clusters
            sigma - width of Gaussian kernel; adjust to alter sensitivity; defaults 0.4
            saveAllSimilarity - flag to save all individual similarity matrices computed; defaults False
            saveWSimilarity - flag to save weighted similarity matrix computed; defaults False
            dirpath - directory to store matrices; defaults None
            verbose - verbosity of function; defaults 0
            no_of_jobs - cores to use to perform computation; defaults 1

        OUTPUT:
            outputPolydata - polydata containing information from clustering to be written into VTK
            clusterIdx - array containing cluster that each fiber belongs to
            colour - array containing the RGB value assigned to the scalar
            centroids - array containing the centroids for each cluster
            fiberData - tree containing spatial and quantitative information of fibers
        """

        no_of_eigvec = k_clusters

        noFibers = inputVTK.GetNumberOfLines()
        if noFibers == 0:
            print "\nERROR: Input data has 0 fibers!"
            return
        elif verbose == 1:
            print "\nStarting clustering..."
            print "No. of fibers:", noFibers
            print "No. of clusters:", k_clusters

        fiberData = fibers.FiberTree()
        fiberData.convertFromVTK(inputVTK, pts_per_fiber, verbose)
        for i in range(len(scalarTypeList)):
            fiberData.addScalar(inputVTK, scalarDataList[i], scalarTypeList[i], pts_per_fiber)

        # 1. Compute similarty matrix
        W = _weightedSimilarity(fiberData, scalarTypeList, scalarWeightList,
                                                    sigma, saveAllSimilarity, pts_per_fiber, dirpath, no_of_jobs)

        if saveWSimilarity is True:
            if dirpath is None:
                dirpath = os.getcwd()
            else:
                if not os.path.exists(dirpath):
                    os.makedirs(dirpath)

            misc.saveMatrix(dirpath, W, 'Weighted')

        # 2. Compute degree matrix
        D = _degreeMatrix(W)

        # 3. Compute unnormalized Laplacian
        L = D - W

        # 4. Compute normalized Laplacian (random-walk)
        Lrw = np.dot(np.diag(np.divide(1, np.sum(D, 0))), L)

        # 5. Compute eigenvalues and eigenvectors of generalized eigenproblem
        # Sort by ascending eigenvalue
        eigval, eigvec = np.linalg.eig(Lrw)
        idx = eigval.argsort()
        eigval, eigvec = eigval[idx], eigvec[:, idx]

        # 6. Compute information for clustering using "N" number of smallest eigenvalues
        U = eigvec[:, 0:no_of_eigvec]
        U = U.real

        # 7. Find clusters using K-means clustering
        centroids, clusterIdx = scipy.cluster.vq.kmeans2(U, k_clusters, iter=20, minit='points')
        centroids, clusterIdx = _sortLabel(centroids, clusterIdx)

        if no_of_eigvec <= 1:
            print('Not enough eigenvectors selected!')
        elif no_of_eigvec == 2:
            temp = eigvec[:, 0:3]
            temp = temp.astype('float')
            colour = _cluster_to_rgb(temp)
            del temp
        else:
            colour = _cluster_to_rgb(centroids)

        # 8. Return results
        # Create model with user / default number of chosen samples along fiber
        outputData = fiberData.convertToVTK()

        outputPolydata = _format_outputVTK(outputData, clusterIdx, colour)

        # 9. Also add measurements from those used to cluster
        for i in range(len(scalarTypeList)):
            outputPolydata = addScalarToVTK(outputPolydata, fiberData, scalarTypeList[i])

        return outputPolydata, clusterIdx, colour, centroids, fiberData

def addScalarToVTK(polyData, fiberTree, scalarType, fidxes=None):
    """
    Add scalar to all polydata points to be converted to .vtk file.
    This function is different from scalars.addScalar, which only considers point
    used in sampling of fiber.

    INPUT:
        polyData - polydata for scalar measurements to be added to
        fiberTree - the tree containing polydata information
        scalarType - type of quantitative measurement to be aded to polydata
        fidxes - array with fiber indices pertaining to scalar data of extracted fibers; default none

    OUTPUT:
        polydata - updated polydata with quantitative information
    """

    data = vtk.vtkFloatArray()
    data.SetName(scalarType.split('/', -1)[-1])

    if fidxes is None:
        for fidx in range(0, polyData.GetNumberOfLines()):
            for pidx in range(0, fiberTree.pts_per_fiber):
                scalarValue = fiberTree.fiberTree[fidx][pidx][scalarType]
                data.InsertNextValue(float(scalarValue))
    else:
        for fidx in fidxes:
            for pidx in range(0, fiberTree.pts_per_fiber):
                scalarValue = fiberTree.fiberTree[fidx][pidx][scalarType]
                data.InsertNextValue(float(scalarValue))

    polyData.GetPointData().AddArray(data)

    return polyData

def extractCluster(inputVTK, clusterIdx, label, pts_per_fiber):
    """
    Extracts a cluster corresponding to the label provided.

    INPUT:
        inputVTK - polydata to extract cluster from
        clusterIdx - labels pertaining to fibers of inputVTK
        label - label of cluster to be extracted
        pts_per_fiber - number of samples to take along fiber

    OUTPUT:
        polyData - extracted cluster in polydata format; no information is retained
    """
    fiberTree = fibers.FiberTree()
    fiberTree.convertFromVTK(inputVTK, pts_per_fiber)

    cluster = fiberTree.getFibers(np.where(clusterIdx == label)[0])
    cluster = fibers.convertFromTuple(cluster)
    polyData = cluster.convertToVTK()

    return polyData

def _pairwiseDistance_matrix(fiberTree, pts_per_fiber, no_of_jobs):
    """ *INTERNAL FUNCTION*
    Used to compute an NxN distance matrix for all fibers (N) in the input data.

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        pts_per_fiber - number of samples along a fiber
        no_of_jobs - cores to use to perform computation

    OUTPUT:
        distances - NxN matrix containing distances between fibers
    """

    distances = Parallel(n_jobs=no_of_jobs)(
            delayed(distance.fiberDistance)(fiberTree.getFiber(fidx),
                    fiberTree.getFibers(range(fiberTree.no_of_fibers)))
            for fidx in range(0, fiberTree.no_of_fibers))

    distances = np.array(distances)
    # Normalize between 0 and 1
    distances = sklearn.preprocessing.MinMaxScaler().fit_transform(distances)

    if np.diag(distances).all() != 0.0:
        print('Diagonals in distance matrix are not equal to 0')
        exit()

    return distances

def _pairwiseSimilarity_matrix(fiberTree, sigma, pts_per_fiber, no_of_jobs):
    """ *INTERNAL FUNCTION*
    Computes an NxN similarity matrix for all fibers (N) in the input data.

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        sigma - width of Gaussian kernel; adjust to alter
        pts_per_fiber - number of samples along a fiber
        no_of_jobs - cores to use to perform computation

    OUTPUT:
        similarity - NxN matrix containing similarity between fibers based on geometry
    """

    distances = _pairwiseDistance_matrix(fiberTree, pts_per_fiber, no_of_jobs)

    sigmasq = np.square(sigma)
    similarities = distance.gausKernel_similarity(distances, sigmasq)

    similarities = np.array(similarities)

    if np.diag(similarities).all() != 1.0:
        print('Diagonals in similarity matrix are not equal to 1')
        exit()

    return similarities

def _pairwiseQDistance_matrix(fiberTree, scalarType, pts_per_fiber, no_of_jobs):
    """ *INTERNAL FUNCTION*
    Computes the "pairwise distance" between quantitative points along a fiber.

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        scalarType - type of quantitative measurements to be used for computation
        pts_per_fiber - number of sample along a fiber
        no_of_jobs - cores to use to perform computation

    OUTPUT:
        qDistances - NxN matrix containing pairwise distances between fibers
    """

    no_of_fibers = fiberTree.no_of_fibers

    qDistances = Parallel(n_jobs=no_of_jobs)(
            delayed(distance.scalarDistance)(
                fiberTree.getScalar(fidx, scalarType),
                fiberTree.getScalars(range(no_of_fibers), scalarType))
            for fidx in range(0, no_of_fibers)
    )

    qDistances = np.array(qDistances)

    # Normalize distance measurements
    qDistances = sklearn.preprocessing.MinMaxScaler().fit_transform(qDistances)

    if np.diag(qDistances).all() != 0.0:
        print('Diagonals in distance matrix are not equal to 0')
        exit()

    return qDistances

def _pairwiseQSimilarity_matrix(fiberTree, scalarType, sigma, pts_per_fiber,
                                                      no_of_jobs):
    """ *INTERNAL FUNCTION*
    Computes the similarity between quantitative points along a fiber.

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        scalarType - type of quantitative measurements to be used for computation
        sigma - width of Gaussian kernel; adjust to alter sensitivity
        no_of_jobs - cores to use to perform computation
        pts_per_fiber - number of samples along a fiber

    OUTPUT:
        qSimilarity - NxN matrix containing similarity of quantitative measurements between fibers
    """

    qDistances = _pairwiseQDistance_matrix(fiberTree, scalarType, pts_per_fiber, no_of_jobs)

    sigmasq = np.square(sigma)
    qSimilarity = distance.gausKernel_similarity(qDistances, sigmasq)

    qSimilarity = np.array(qSimilarity)

    if np.diag(qSimilarity).all() != 1.0:
        print('Diagonals in similarity marix are not equal to 1')
        exit()

    return qSimilarity

def _degreeMatrix(inputMatrix):
    """ *INTERNAL FUNCTION*
    Computes the degree matrix, D.

    INPUT:
        inputMatrix - adjacency matrix to be used for computation

    OUTPUT:
        degMat - degree matrix to be used to compute Laplacian matrix
    """

    # Determine the degree matrix
    degMat = np.diag(np.sum(inputMatrix, 0))

    return degMat

def _cluster_to_rgb(data):
    """ *INTERNAL FUNCTION*
    Generate cluster color from first three components of data

    INPUT:
        data - information used to calculate RGB colours; typically eigenvectors or centroids are used

    OUTPUT:
        colour - array containing the RGB values to colour clusters
    """

    colour = data[:, 0:3]

    # Normalize color
    colourMag = np.sqrt(np.sum(np.square(colour), 1))
    colour = np.divide(colour.T, colourMag).T

    # Convert range from 0 to 255
    colour = 127.5 + (colour * 127.5)

    return colour.astype('int')

def _format_outputVTK(polyData, clusterIdx, colour):
    """ *INTERNAL FUNCTION*
    Formats polydata with cluster index and colour.

    INPUT:
        polyData - polydata for information to be applied to
        clusterIdx - cluster indices to be applied to each fiber within the polydata model
        colour - colours to be applied to each fiber within the polydata model

    OUTPUT:
        polyData - updated polydata with cluster and colour information
    """

    dataColour = vtk.vtkUnsignedCharArray()
    dataColour.SetNumberOfComponents(3)
    dataColour.SetName('Colour')

    clusterNumber = vtk.vtkIntArray()
    clusterNumber.SetName('ClusterNumber')

    for fidx in range(0, polyData.GetNumberOfLines()):
        dataColour.InsertNextTuple3(
                colour[clusterIdx[fidx], 0], colour[clusterIdx[fidx], 1], colour[clusterIdx[fidx], 2])
        clusterNumber.InsertNextTuple1(int(clusterIdx[fidx]))

    polyData.GetCellData().AddArray(dataColour)
    polyData.GetCellData().AddArray(clusterNumber)

    return polyData

def _weightedSimilarity(fiberTree, scalarTypeList=[], scalarWeightList=[],
                                        sigma=0.4, saveAllSimilarity=False, pts_per_fiber=20, dirpath=None,
                                        no_of_jobs=1):
    """ *INTERNAL FUNCTION*
    Computes and returns a single weighted similarity matrix.
    Weight list should include weight for distance and sum to 1.

    INPUT:
        inputVTK - input polydata
        scalarTree - tree containing scalar data for similarity measurements
        scalarTypeList - list containing scalar type for similarity measurements; defaults empty
        scalarWeightList - list containing scalar weights for similarity measurements; defaults empty
        sigma - width of Gaussian kernel; adjust to alter sensitivity; defaults 0.4
        saveAllSimilarity - flag to save all individual similarity matrices computed; defaults 0 (off)
        dirpath - directory to store similarity matrices
        no_of_jobs - cores to use to perform computation; defaults 1

    OUTPUT:
        wSimilarity - matrix containing the computed weighted similarity
    """

    if ((scalarWeightList == []) & (scalarTypeList != [])):
        print "\nNo weights given for provided measurements! Exiting..."
        exit()

    elif ((scalarWeightList != []) & (scalarTypeList == [])):
        print "\nPlease also specify measurement(s) type. Exiting..."
        exit()

    elif ((scalarWeightList == [])) & ((scalarTypeList == [])):
        print "\nNo measurements provided!"
        print "\nCalculating similarity based on geometry."
        wSimilarity = _pairwiseSimilarity_matrix(fiberTree, sigma, pts_per_fiber, no_of_jobs)

        if dirpath is None:
            dirpath = os.getcwd()
        else:
            if not os.path.exists(dirpath):
                os.makedirs(dirpath)

        misc.saveMatrix(dirpath, wSimilarity, 'Geometry')

    else:   # Calculate weighted similarity

        if np.sum(scalarWeightList) != 1.0:
            print '\nWeights given do not sum 1. Exiting...'
            exit()

        wSimilarity = _pairwiseSimilarity_matrix(fiberTree, sigma, pts_per_fiber,
                                                                            no_of_jobs) * scalarWeightList[0]

        if saveAllSimilarity is True:
            if dirpath is None:
                dirpath = os.getcwd()
            else:
                if not os.path.exists(dirpath):
                    os.makedirs(dirpath)

            matrixType = scalarTypeList[0].split('/', -1)[-1]
            matrixType = matrixType[:-2] + 'distance'
            misc.saveMatrix(dirpath, wSimilarity, matrixType)

        for i in range(len(scalarTypeList)):
            similarity = _pairwiseQSimilarity_matrix(fiberTree,
                scalarTypeList[i], sigma, pts_per_fiber, no_of_jobs)

            if saveAllSimilarity is True:
                if dirpath is None:
                    dirpath = os.getcwd()
                else:
                    if not os.path.exists(dirpath):
                        os.makedirs(dirpath)

                matrixType = scalarTypeList[i].split('/', -1)[-1]
                misc.saveMatrix(dirpath, similarity, matrixType)

            wSimilarity += similarity * scalarWeightList[i+1]

        del similarity

    if np.diag(wSimilarity).all() != 1.0:
        print('Diagonals of weighted similarity are not equal to 1')
        exit()

    return wSimilarity

def _sortLabel(centroids, clusterIdx):
    """ *INTERNAL FUNCTION*
    Sort the cluster label by fiber count.

    INPUT:
        centroids - array of centroids to be sorted
        clusterIdx - array containing cluster indices to be sorted

    OUTPUT:
        newCentroids - array of sorted centroids
        newClusters - array of sorted clusters
    """

    uniqueClusters, countClusters = np.unique(clusterIdx, return_counts=True)
    sortedClusters = np.argsort(-countClusters)

    newClusters = np.copy(clusterIdx)
    newCentroids = np.copy(centroids)

    for i in range(len(sortedClusters)):
        newIdx = np.where(sortedClusters == i)
        newClusters[clusterIdx == i] = newIdx[0][0]
        newCentroids[i, :] = centroids[newIdx[0][0], :]

    return newCentroids, newClusters
