""" cluster.py

Module containing classes and functions used to cluster fibers and modify
parameters pertaining to clusters.

"""

import numpy as np
import os, scipy.cluster, sklearn.preprocessing
from joblib import Parallel, delayed
from joblib.pool import has_shareable_memory
from sys import exit

import fibers, distance, misc, prior
import vtk

def spectralClustering(fiberData, scalarDataList=[], scalarTypeList=[], scalarWeightList=[],
                                    pts_per_fiber=20, k_clusters=50, sigma=0.2, saveAllSimilarity=False,
                                    saveWSimilarity=False, dirpath=None, verbose=0, no_of_jobs=1):
        """
        Clustering of fibers based on pairwise fiber similarity.
        See paper: "A tutorial on spectral clustering" (von Luxburg, 2007)

        If no scalar data provided, clustering performed based on geometry.
        First element of scalarWeightList should be weight placed for geometry, followed by order
        given in scalarTypeList These weights should sum to 1.0 (weights given as a decimal value).
        ex.  scalarDataListiberData
              scalarTypeList = [FA, T1]
              scalarWeightList = [Geometry, FA, T1]

        INPUT:
            fiberData - fiber tree of tractography data to be clustered
            scalarDataList - list containing scalar data for similarity measurements; defaults empty
            scalarTypeList - list containing scalar type for similarity measurements; defaults empty
            scalarWeightList - list containing scalar weights for similarity measurements; defaults empty
            pts_per_fiber - number of samples to take along each fiber
            k_clusters - number of clusters via k-means clustering; defaults 10 clusters
            sigma - width of Gaussian kernel; adjust to alter sensitivity; defaults 0.4
            saveAllSimilarity - flag to save all individual similarity matrices computed; defaults False
            saveWSimilarity - flag to save weighted similarity matrix
            dirpath - directory to store files; defaults None
            verbose - verbosity of function; defaults 0
            no_of_jobs - cores to use to perform computation; defaults 1

        OUTPUT:
            outputPolydata - polydata containing information from clustering
            clusterIdx - array containing cluster that each fiber belongs to
            fiberData - tree containing spatial and quantitative information of fibers
            rejIdx - indices of fibers considered to be outliers
        """
        if dirpath is None:
            dirpath = os.getcwd()

        matpath = dirpath + '/matrices'
        if not os.path.exists(matpath):
            os.makedirs(matpath)

        noFibers = fiberData.no_of_fibers
        if noFibers == 0:
            print "\nERROR: Input data has 0 fibers!"
            raise ValueError
        elif verbose == 1:
            print "\nStarting clustering..."
            print "No. of fibers:", noFibers
            print "No. of clusters:", k_clusters

        # 1. Compute similarty matrix
        W = _pairwiseWeightedSimilarity(fiberData, scalarTypeList, scalarWeightList,
                                                    sigma, saveAllSimilarity, pts_per_fiber, matpath, no_of_jobs)

        # Outlier detection
        W, rejIdx = _outlierSimDetection(W)

        if saveWSimilarity is True:
            misc.saveMatrix(matpath, W, 'Weighted')

        # 2. Compute degree matrix
        D = _degreeMatrix(W)

        # 3. Compute unnormalized Laplacian
        L = D - W

        # 4. Compute normalized Laplacian (random-walk)
        Lrw = np.dot(np.diag(np.divide(1, np.sum(D, 0))), L)

        # 5. Compute eigenvalues and eigenvectors of generalized eigenproblem
        # Sort by ascending eigenvalue
        eigval, eigvec = np.linalg.eigh(Lrw)
        idx = eigval.argsort()
        eigval, eigvec = eigval[idx], eigvec[:, idx]
        misc.saveEig(dirpath, eigval, eigvec)

        # 6. Compute information for clustering using "N" number of smallest eigenvalues
        # Skips first eigenvector, no information obtained
        if k_clusters > eigvec.shape[0]:
            print '\nNumber of user selected clusters greater than number of eigenvectors.'
            print 'Clustering with maximum number of available eigenvectors.'
            emvec = eigvec[:, 1:eigvec.shape[0]]
        elif k_clusters == eigvec.shape[0]:
            emvec = eigvec[:, 1:k_clusters]
        else:
            emvec = eigvec[:, 1:k_clusters + 1]

        # 7. Find clusters using K-means clustering
        centroids, clusterIdx = scipy.cluster.vq.kmeans2(emvec, k_clusters, iter=50,
                                                                                        minit='points')
        centroids, clusterIdx = _sortLabel(centroids, clusterIdx)
        fiberData.addClusterInfo(clusterIdx, centroids)

        if k_clusters <= 1:
            print "\nNot enough eigenvectors selected!"
            raise ValueError
        elif k_clusters == 2:
            temp = eigvec[:, 0:3]
            temp = temp.astype('float')
            colour = _cluster_to_rgb(temp)
            del temp
        else:
            colour = _cluster_to_rgb(centroids)

        # 8. Return results
        # Create model with user / default number of chosen samples along fiber
        outputData = fiberData.convertToVTK(rejIdx)
        outputPolydata = _format_outputVTK(outputData, clusterIdx, colour, centroids)

        # 9. Also add measurements from those used to cluster
        for i in range(len(scalarTypeList)):
            outputPolydata = addScalarToVTK(outputPolydata, fiberData, scalarTypeList[i],
                                            rejIdx=rejIdx)

        return outputPolydata, clusterIdx, fiberData, rejIdx

def spectralPriorCluster(fiberData, priorVTK, scalarDataList=[], scalarTypeList=[],
                                    scalarWeightList=[], sigma=0.4, saveAllSimilarity=False,
                                    saveWSimilarity=False, dirpath=None, verbose=0, no_of_jobs=1):
        """
        Clustering of fibers based on pairwise fiber similarity using previously clustered fibers
        via a Nystrom-like method.
        See paper: "A tutorial on spectral clustering" (von Luxburg, 2007)
                          "Spectral grouping using the Nystrom method" (Fowles et al., 2004)

        If no scalar data provided, clustering performed based on geometry.
        First element of scalarWeightList should be weight placed for geometry, followed by order
        given in scalarTypeList. These weights should sum to 1.0 (weights given as a decimal value).
        ex. scalarDataList
              scalarTypeList = [FA, T1]
              scalarWeightList = [Geometry, FA, T1]

        INPUT:
            fiberData - fiber tree containing tractography data to be clustered
            priorVTK - prior polydata file
            scalarDataList - list containing scalar data for similarity measurements; defaults empty
            scalarTypeList - list containing scalar type for similarity measurements; defaults empty
            scalarWeightList - list containing scalar weights for similarity measurements; defaults empty
            sigma - width of Gaussian kernel; adjust to alter sensitivity; defaults 0.4
            saveAllSimilarity - flag to save all individual similarity matrices computed; defaults False
            saveWSimilarity - flag to save weighted similarity matrix computed; defaults False
            dirpath - directory to store files; defaults None
            verbose - verbosity of function; defaults 0
            no_of_jobs - cores to use to perform computation; defaults 1

        OUTPUT:
            outputPolydata - polydata containing information from clustering to be written into VTK
            clusterIdx - array containing cluster that each fiber belongs to
            fiberData - tree containing spatial and quantitative information of fibers
        """
        if dirpath is None:
            dirpath = os.getcwd()

        matpath = dirpath + '/matrices'
        if not os.path.exists(matpath):
            os.makedirs(matpath)

        priorData, priorCentroids = prior.load(priorVTK)
        priorPath = os.path.split(priorVTK)[0]

        if not os.path.exists(priorPath + '/eigval.npy'):
            print "Eigenvalue binary file does not exist"
            raise IOError
        elif not os.path.exists(priorPath + '/eigvec.npy'):
            print "Eigenvector binary file does not exist"
            raise IOError
        else:
            eigval, eigvec = prior.loadEig(priorPath, 'eigval.npy', 'eigvec.npy')

        k_clusters = len(priorCentroids)
        pts_per_fiber = int(priorData.pts_per_fiber)

        noFibers = fiberData.no_of_fibers
        nopriorFibers = int(priorData.no_of_fibers)
        if noFibers == 0 or nopriorFibers == 0:
            print "\nERROR: Input data(s) has 0 fibers!"
            raise ValueError
        elif verbose == 1:
            print "\nStarting clustering..."
            print "No. of fibers:", noFibers
            print "No. of clusters:", k_clusters

        # 1. Compute similarty matrix
        W = _priorWeightedSimilarity(fiberData, priorData, scalarTypeList, scalarWeightList,
                                                    sigma, saveAllSimilarity, pts_per_fiber, matpath, no_of_jobs)

        # 2. Compute inverse of eigenvalues
        invEigval = np.diag(np.divide(1, eigval))

        # 3. Compute new eigenvectors vectors in feature space
        nEigvec = np.dot(np.dot(W, eigvec), invEigval)

        # 4. Compute information for clustering using "N" number of smallest eigenvalues
        # Skips first eigenvector, no information obtained
        if k_clusters > nEigvec.shape[0]:
            print 'Number of user selected clusters greater than number of eigenvectors.'
            print 'Clustering with maximum number of available eigenvectors.'
            emvec = nEigvec[:, 1:nEigvec.shape[0]]
        elif k_clusters == eigvec.shape[0]:
            emvec = nEigvec[:, 1:k_clusters]
        else:
            emvec = nEigvec[:, 1:k_clusters + 1]
        emvec = emvec.real

        # 5. Find clusters using K-means clustering
        clusterIdx, dist = scipy.cluster.vq.vq(emvec, priorCentroids)

        fiberData.addClusterInfo(clusterIdx, priorCentroids)

        if k_clusters <= 1:
            print('Not enough eigenvectors selected!')
            raise ValueError
        elif k_clusters == 2:
            temp = eigvec[:, 0:3]
            temp = temp.astype('float')
            colour = _cluster_to_rgb(temp)
            del temp
        else:
            colour = _cluster_to_rgb(priorCentroids)

        # 8. Return results
        # Create model with user / default number of chosen samples along fiber
        # Outlier rejection based on distance from centroid
        W, rejIdx, clusterIdx = _distOutlierDetection(W, dist, clusterIdx)

        if saveWSimilarity is True:
            misc.saveMatrix(matpath, W, 'Weighted')

        outputData = fiberData.convertToVTK(rejIdx)
        outputPolydata = _format_outputVTK(outputData, clusterIdx, colour, priorCentroids)

        # 9. Also add measurements from those used to cluster
        for i in range(len(scalarTypeList)):
            outputPolydata = addScalarToVTK(outputPolydata, fiberData, scalarTypeList[i])

        return outputPolydata, clusterIdx, fiberData, rejIdx

def addScalarToVTK(polyData, fiberTree, scalarType, fidxes=None, rejIdx=[]):
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
        for fidx in range(0, fiberTree.no_of_fibers):
            if fidx in rejIdx:
                continue
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

    temp = Parallel(n_jobs=no_of_jobs, backend="threading")(
            delayed(distance.fiberDistance, has_shareable_memory)(fiberTree.getFiber(fidx),
                        fiberTree.getFibers(range(fidx, fiberTree.no_of_fibers)))
            for fidx in range(0, fiberTree.no_of_fibers))
    temp = np.array(temp)

    distances = np.zeros((fiberTree.no_of_fibers, fiberTree.no_of_fibers))
    for i in range(0, fiberTree.no_of_fibers):
        idx = 0
        for j in range(i, fiberTree.no_of_fibers):
            distances[i][j] = temp[i][idx]
            distances[j][i] = temp[i][idx]
            idx += 1
    del temp

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

    temp = Parallel(n_jobs=no_of_jobs, backend="threading")(
            delayed(distance.scalarDistance, has_shareable_memory)(
                fiberTree.getScalar(fidx, scalarType),
                fiberTree.getScalars(range(fidx, fiberTree.no_of_fibers), scalarType))
            for fidx in range(0, fiberTree.no_of_fibers)
    )
    temp = np.array(temp)

    qDistances = np.zeros((fiberTree.no_of_fibers, fiberTree.no_of_fibers))
    for i in range(0, fiberTree.no_of_fibers):
        idx = 0
        for j in range(i, fiberTree.no_of_fibers):
            qDistances[i][j] = temp[i][j]
            qDistances[j][i] = temp[i][j]
            idx += 1
    del temp

    # Normalize distance measurements
    qDistances = sklearn.preprocessing.MinMaxScaler().fit_transform(qDistances)

    if np.diag(qDistances).all() != 0.0:
        print "Diagonals in distance matrix are not equal to 0"
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
        print "Diagonals in similarity marix are not equal to 1"
        exit()

    return qSimilarity

def _priorDistance_matrix(fiberTree, priorTree, pts_per_fiber, no_of_jobs):
    """ *INTERNAL FUNCTION*
    Used to compute an distance matrix for all fibers (N) in the input data through
    comparison with previously clustered data

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        priorTree - tree containing spatial and quantitative info from prev. clustered fibers
        pts_per_fiber - number of samples along a fiber
        no_of_jobs - cores to use to perform computation

    OUTPUT:
        distances - matrix containing distances between fibers
    """

    distances = Parallel(n_jobs=no_of_jobs, backend="threading")(
            delayed(distance.fiberDistance, has_shareable_memory)(fiberTree.getFiber(fidx),
                    priorTree.getFibers(range(priorTree.no_of_fibers)))
            for fidx in range(0, fiberTree.no_of_fibers))

    distances = np.array(distances)

    # Normalize between 0 and 1
    distances = sklearn.preprocessing.MinMaxScaler().fit_transform(distances)

    return distances

def _priorSimilarity_matrix(fiberTree, priorTree, sigma, pts_per_fiber, no_of_jobs):
    """ *INTERNAL FUNCTION*
    Computes a similarity matrix for all fibers (N) in the input data to previously clustered fibers

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        priorTree - tree containing spatial and quantitative info from previously clustered fibers
        sigma - width of Gaussian kernel; adjust to alter
        pts_per_fiber - number of samples along a fiber
        no_of_jobs - cores to use to perform computation

    OUTPUT:
        similarity - matrix containing similarity between fibers based on geometry
    """

    distances = _priorDistance_matrix(fiberTree, priorTree, pts_per_fiber, no_of_jobs)

    sigmasq = np.square(sigma)
    similarities = distance.gausKernel_similarity(distances, sigmasq)

    similarities = np.array(similarities)

    return similarities

def _priorQDistance_matrix(fiberTree, priorTree, scalarType, pts_per_fiber, no_of_jobs):
    """ *INTERNAL FUNCTION*
    Computes the "pairwise distance" between quantitative points along a fiber and previously
    clustered fibers

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        priorTree - tree containing information on previously clustered fibers
        scalarType - type of quantitative measurements to be used for computation
        pts_per_fiber - number of sample along a fiber
        no_of_jobs - cores to use to perform computation

    OUTPUT:
        qDistances - matrix containing pairwise distances between fibers
    """

    qDistances = Parallel(n_jobs=no_of_jobs, backend="threading")(
            delayed(distance.scalarDistance, has_shareable_memory)(
                fiberTree.getScalar(fidx, scalarType),
                priorTree.getScalars(range(priorTree.no_of_fibers), scalarType))
            for fidx in range(0, fiberTree.no_of_fibers)
    )

    qDistances = np.array(qDistances)

    # Normalize distance measurements
    qDistances = sklearn.preprocessing.MinMaxScaler().fit_transform(qDistances)

    return qDistances

def _priorQSimilarity_matrix(fiberTree, priorTree, scalarType, sigma, pts_per_fiber,
                                                      no_of_jobs):
    """ *INTERNAL FUNCTION*
    Computes the similarity between quantitative points along a fiber and previously clustered
    fibers

    INPUT:
        fiberTree - tree containing spatial and quantitative information of fibers
        priorsTree - tree containing information on previously clustered fibers
        scalarType - type of quantitative measurements to be used for computation
        sigma - width of Gaussian kernel; adjust to alter sensitivity
        no_of_jobs - cores to use to perform computation
        pts_per_fiber - number of samples along a fiber

    OUTPUT:
        qSimilarity - matrix containing similarity of quantitative measurements between fibers
    """

    qDistances = _priorQDistance_matrix(fiberTree, priorTree, scalarType, pts_per_fiber, no_of_jobs)

    sigmasq = np.square(sigma)
    qSimilarity = distance.gausKernel_similarity(qDistances, sigmasq)

    qSimilarity = np.array(qSimilarity)

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

def _format_outputVTK(polyData, clusterIdx, colour, centroids, rejIdx=[]):
    """ *INTERNAL FUNCTION*
    Formats polydata with cluster index and colour.

    INPUT:
        polyData - polydata for information to be applied to
        clusterIdx - cluster indices to be applied to each fiber within the polydata model
        colour - colours to be applied to each fiber within the polydata model
        centroid - centroid location to associated with each cluster

    OUTPUT:
        polyData - updated polydata with cluster and colour information
    """

    dataColour = vtk.vtkUnsignedCharArray()
    dataColour.SetNumberOfComponents(3)
    dataColour.SetName('Colour')

    clusterLabel = vtk.vtkIntArray()
    clusterLabel.SetNumberOfComponents(1)
    clusterLabel.SetName('ClusterLabel')

    centroid = vtk.vtkFloatArray()
    centroid.SetNumberOfComponents(centroids.shape[1])
    centroid.SetName('Centroid')

    for fidx in range(0, polyData.GetNumberOfLines()):
        if fidx in rejIdx:
            continue
        dataColour.InsertNextTuple3(
                colour[clusterIdx[fidx], 0], colour[clusterIdx[fidx], 1], colour[clusterIdx[fidx], 2])
        clusterLabel.InsertNextTuple1(int(clusterIdx[fidx]))
        centroid.InsertNextTuple(centroids[clusterIdx[fidx], :])

    polyData.GetCellData().AddArray(dataColour)
    polyData.GetCellData().AddArray(clusterLabel)
    polyData.GetCellData().AddArray(centroid)

    return polyData

def _pairwiseWeightedSimilarity(fiberTree, scalarTypeList=[], scalarWeightList=[],
                                        sigma=0.2, saveAllSimilarity=False, pts_per_fiber=20, dirpath=None,
                                        no_of_jobs=1):
    """ *INTERNAL FUNCTION*
    Computes and returns a single weighted similarity matrix.
    Weight list should include weight for distance and sum to 1.

    INPUT:
        fiberTree - tree containing scalar data for similarity measurements
        scalarTypeList - list containing scalar type for similarity measurements; defaults empty
        scalarWeightList - list containing scalar weights for similarity measurements; defaults empty
        sigma - width of Gaussian kernel; adjust to alter sensitivity; defaults 0.4
        saveAllSimilarity - flag to save all individual similarity matrices computed; defaults 0 (off)
        dirpath - directory to store similarity matrices
        no_of_jobs - cores to use to perform computation; defaults 1

    OUTPUT:
        wSimilarity - matrix containing the computed weighted similarity
    """

    if ((scalarWeightList == []) and (scalarTypeList != [])):
        print "\nNo weights given for provided measurements! Exiting..."
        exit()

    elif ((scalarWeightList != []) and (scalarTypeList == [])):
        print "\nPlease also specify measurement(s) type. Exiting..."
        exit()

    elif (((scalarWeightList == [])) and ((scalarTypeList == []))) or (scalarWeightList[0] == 1):
        print "\nCalculating similarity based on geometry."
        wSimilarity = _pairwiseSimilarity_matrix(fiberTree, sigma, pts_per_fiber, no_of_jobs)

        if dirpath is None:
            dirpath = os.getcwd()

        misc.saveMatrix(dirpath, wSimilarity, 'Geometry')

    else:   # Calculate weighted similarity

        if np.sum(scalarWeightList) != 1.0:
            print '\nWeights given do not sum 1. Exiting...'
            exit()

        wSimilarity = _pairwiseSimilarity_matrix(fiberTree, sigma, pts_per_fiber,
                                                                            no_of_jobs)

        if saveAllSimilarity is True:
            if dirpath is None:
                dirpath = os.getcwd()

            matrixType = scalarTypeList[0].split('/', -1)[-1]
            matrixType = matrixType[:-2] + 'geometry'
            misc.saveMatrix(dirpath, wSimilarity, matrixType)

        wSimilarity = wSimilarity * scalarWeightList[0]

        for i in range(len(scalarTypeList)):
            similarity = _pairwiseQSimilarity_matrix(fiberTree,
                scalarTypeList[i], sigma, pts_per_fiber, no_of_jobs)

            if saveAllSimilarity is True:
                if dirpath is None:
                    dirpath = os.getcwd()

                matrixType = scalarTypeList[i].split('/', -1)[-1]
                misc.saveMatrix(dirpath, similarity, matrixType)

            wSimilarity += similarity * scalarWeightList[i+1]

        del similarity

    if np.diag(wSimilarity).all() != 1.0:
        print "Diagonals of weighted similarity are not equal to 1"
        exit()

    return wSimilarity

def _priorWeightedSimilarity(fiberTree, priorTree, scalarTypeList=[], scalarWeightList=[],
                                        sigma=0.4, saveAllSimilarity=False, pts_per_fiber=20, dirpath=None,
                                        no_of_jobs=1):
    """ *INTERNAL FUNCTION*
    Computes and returns a single weighted similarity matrix.
    Weight list should include weight for distance and sum to 1.

    INPUT:
        fiberTree - tree containing scalar data for similarity measurements
        priorTree - tree containing previously clustered tract information
        scalarTypeList - list containing scalar type for similarity measurements; defaults empty
        scalarWeightList - list containing scalar weights for similarity measurements; defaults empty
        sigma - width of Gaussian kernel; adjust to alter sensitivity; defaults 0.4
        saveAllSimilarity - flag to save all individual similarity matrices computed; defaults 0 (off)
        dirpath - directory to store similarity matrices
        no_of_jobs - cores to use to perform computation; defaults 1

    OUTPUT:
        wSimilarity - matrix containing the computed weighted similarity
    """

    if ((scalarWeightList == []) and (scalarTypeList != [])):
        print "\nNo weights given for provided measurements! Exiting..."
        exit()

    elif ((scalarWeightList != []) and (scalarTypeList == [])):
        print "\nPlease also specify measurement(s) type. Exiting..."
        exit()

    elif ((scalarWeightList == []) and (scalarTypeList == [])) or (scalarWeightList[0] == 1):
        print "\nCalculating similarity based on geometry."
        wSimilarity = _priorSimilarity_matrix(fiberTree, priorTree, sigma, pts_per_fiber, no_of_jobs)

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

        wSimilarity = _priorSimilarity_matrix(fiberTree, priorTree, sigma, pts_per_fiber,
                                                                            no_of_jobs)

        if saveAllSimilarity is True:
            if dirpath is None:
                dirpath = os.getcwd()

            matrixType = scalarTypeList[0].split('/', -1)[-1]
            matrixType = matrixType[:-2] + 'geometry'
            misc.saveMatrix(dirpath, wSimilarity, matrixType)

        wSimilarity = wSimilarity * scalarWeightList[0]

        for i in range(len(scalarTypeList)):
            similarity = _priorQSimilarity_matrix(fiberTree, priorTree,
                scalarTypeList[i], sigma, pts_per_fiber, no_of_jobs)

            if saveAllSimilarity is True:
                if dirpath is None:
                    dirpath = os.getcwd()

                matrixType = scalarTypeList[i].split('/', -1)[-1]
                misc.saveMatrix(dirpath, similarity, matrixType)

            wSimilarity += similarity * scalarWeightList[i+1]

        del similarity

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
        newCentroids[i, :] = centroids[sortedClusters[i]]

    return newCentroids, newClusters

def _outlierSimDetection(W):
    """ * INTERNAL FUNCTION *
    Look for outliers in fibers to reject

    INPUT::
        W - similarity matrix

    OUTPUT:
        W - similarity matrix with removed outliers
        rejIdx - indices of fibers considered outliers

    """

    # Reject fibers that are 2 standard deviations from mean
    W_rowsum = np.sum(W, 0)
    W_outlierthr = np.mean(W_rowsum) - 2.0 * np.std(W_rowsum)

    rejIdx = np.where(W_rowsum < W_outlierthr)[0]
    # Remove outliers from matrix
    W = np.delete(W, rejIdx, 0)
    W = np.delete(W, rejIdx, 1)

    return W, rejIdx

def _distOutlierDetection(W, dist, clusterIdx):
    """ * INTERNAL FUNCTION *
    Look for outlifers in fibers to reject based on distance
    from centroid

    INPUT:
        W - similarity matrix between two different datasets
        dist - array of distance of each fiber to centroid
        clusterIdx - array of cluster labels for each fiber

    OUTPUT:
        W - similarity matrix with removed outliers
        rejIdx - indices of fibers considered outliers
        clusterIdx - array of clusterLabels with removed outliers

    """

    rejIdx = np.where(dist > (np.mean(dist) + 2.0 * np.std(dist)))[0]

    W = np.delete(W, rejIdx, 0)
    clusterIdx = np.delete(clusterIdx, rejIdx)

    return W, rejIdx, clusterIdx