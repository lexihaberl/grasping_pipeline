#! /usr/bin/env python3

import rospy
import actionlib

import moveit_commander
import moveit_msgs.msg
import sys
import geometry_msgs.msg
import tf.transformations
import tf


from grasping_pipeline.msg import ExecuteGraspAction, ExecuteGraspActionResult
from hsrb_interface import Robot, geometry
from std_srvs.srv import Empty
from visualization_msgs.msg import Marker
from math import pi
import numpy as np

from table_plane_extractor.srv import TablePlaneExtractor
from object_detector_msgs.msg import Plane
from v4r_util.util import ros_bb_to_o3d_bb
import open3d as o3d
from open3d_ros_helper import open3d_ros_helper as orh
from sensor_msgs.msg import PointCloud2
import copy
from placement.msg import *
from geometry_msgs.msg import Pose
from enum import IntEnum

class CollisionMethod(IntEnum):
    ADD = 0
    REMOVE = 1
    ATTACH = 2
    DETACH = 3

class ExecuteGraspServer:
    def __init__(self):
        self.robot = Robot()
        self.whole_body = self.robot.try_get('whole_body')
        self.gripper = self.robot.get('gripper')
        self.omni_base = self.robot.try_get('omni_base')
        self.tf = tf.TransformListener()
        self.use_map = rospy.get_param('/use_map', False)

        self.move_group = self.moveit_init()
        self.server = actionlib.SimpleActionServer(
            'execute_grasp', ExecuteGraspAction, self.execute, False)
        self.clear_octomap = rospy.ServiceProxy('/clear_octomap', Empty)

        self.server.start()

    def moveit_init(self):
        """ Initializes MoveIt, sets workspace and creates collision environment

        Returns:
            MoveGroupCommander -- MoveIt interface
        """        
        moveit_commander.roscpp_initialize(sys.argv)
        self.robot_cmd = moveit_commander.RobotCommander()
        self.scene = moveit_commander.PlanningSceneInterface()
        rospy.sleep(1)
        self.group_name = "whole_body"
        move_group = moveit_commander.MoveGroupCommander(self.group_name)
        display_trajectory_publisher = rospy.Publisher('/move_group/display_planned_path',
                                                       moveit_msgs.msg.DisplayTrajectory,
                                                       queue_size=20)
        self.planning_frame = move_group.get_planning_frame()
        self.eef_link = move_group.get_end_effector_link()
        self.group_names = self.robot_cmd.get_group_names()

        t = self.tf.getLatestCommonTime('/odom', '/base_link')
        transform = self.tf.lookupTransform('/odom', '/base_link', t)
        move_group.set_workspace(
            (-1.5 + transform[0][0], -1.5 + transform[0][1], -1, 1.5 + transform[0][0], 1.5 + transform[0][1], 3))

        move_group.allow_replanning(True)
        self.scene.remove_attached_object(self.eef_link)
        self.scene.remove_world_object()
        move_group.clear_pose_targets()
        # move_group.set_num_planning_attempts(5)
        self.create_collision_environment()

        return move_group

    def execute(self, goal):
        res = ExecuteGraspActionResult()
        self.create_collision_environment()

        coll_objects = self.get_table_collision_object()
        self.add_table_collision_object(coll_objects)

        self.clear_octomap()
        plan_found = False
        for grasp_pose in goal.grasp_poses:
            if grasp_pose.header.frame_id == "":
                rospy.loginfo('Not a valid goal. Aborted execution!')
                self.server.set_aborted()
                return

            # add safety_distance to grasp_pose
            q = [grasp_pose.pose.orientation.x, grasp_pose.pose.orientation.y,
                 grasp_pose.pose.orientation.z, grasp_pose.pose.orientation.w]

            approach_vector = qv_mult(q, [0, 0, -1])
            print(approach_vector)
            safety_distance = + \
                rospy.get_param("/safety_distance", default=0.08)
            grasp_pose.pose.position.x = grasp_pose.pose.position.x + \
                safety_distance * approach_vector[0]
            grasp_pose.pose.position.y = grasp_pose.pose.position.y + \
                safety_distance * approach_vector[1]
            grasp_pose.pose.position.z = grasp_pose.pose.position.z + \
                safety_distance * approach_vector[2]

            t = self.tf.getLatestCommonTime(
                '/odom', grasp_pose.header.frame_id)
            grasp_pose.header.stamp = t
            grasp_pose = self.tf.transformPose('/odom', grasp_pose)
            self.add_marker(grasp_pose)
            self.move_group.set_pose_target(grasp_pose)
            plan = self.move_group.plan()[1]
            if len(plan.joint_trajectory.points) > 0:
                plan_found = True
                break

        # abort if no plan is found
        if not plan_found:
            rospy.logerr('no grasp found')
            self.move_group.stop()
            self.move_group.clear_pose_targets()
            res.result.success = False
            self.server.set_aborted(res.result)
            return

        self.move_group.go(wait=True)
        rospy.sleep(0.5)

        self.whole_body.move_end_effector_by_line((0, 0, 1), safety_distance)

        self.gripper.apply_force(0.30)

        # move 5cm in z direction
        pose_vec, pose_quat = self.whole_body.get_end_effector_pose(
            'base_link')
        new_pose_vec = geometry.Vector3(
            pose_vec.x, pose_vec.y, pose_vec.z + 0.05)
        new_pose = geometry.Pose(new_pose_vec, pose_quat)
        self.whole_body.move_end_effector_pose(new_pose)

        self.whole_body.move_end_effector_by_line((0, 0, 1), -safety_distance)
        self.omni_base.go_rel(-0.2, 0.0, 0.0, 10)
        self.whole_body.move_to_neutral()

        # check if object is in gripper
        self.gripper.apply_force(0.50)
        if self.gripper.get_distance() > -0.004:
            res.result.success = True
            self.server.set_succeeded(res.result)

        else:
            res.result.success = False
            rospy.logerr('grasping failed')
            self.gripper.command(1.0)
            self.server.set_aborted()

    def add_table_collision_object(self, coll_objects):
        collisionEnvironment_client = actionlib.SimpleActionClient('PlacementCollisionEnvironment', PlacementCollisionEnvironmentAction)
        collisionEnvironment_client.wait_for_server()
        collisionEnvironment_goal = PlacementCollisionEnvironmentGoal()

        collisionObject_list = list()

        counter = 1

        for obj in coll_objects:
            collisionObject = CollisionObject()
            pose = Pose()
            pose.position.x = obj.center[0]
            pose.position.y = obj.center[1]
            pose.position.z = obj.center[2] - 0.1
            pose.orientation.x = 0.0
            pose.orientation.y = 0.0
            pose.orientation.z = 0.0
            pose.orientation.w = 1.0
            collisionObject.pose = pose
            collisionObject.size_x = obj.extent[0]
            collisionObject.size_y = obj.extent[1]
            collisionObject.size_z = obj.extent[2]
            collisionObject.name = "collision_object" + str(counter)
            collisionObject.frame = "map"
            collisionObject.method = CollisionMethod.ADD
            collisionObject_list.append(collisionObject)
            counter += 1

        # add floor
        pose = self.omni_base.get_pose()
        collisionObject = CollisionObject()
        floor_pose = Pose()
        floor_pose.position.x = pose.pos.x
        floor_pose.position.y = pose.pos.y
        floor_pose.position.z = -0.07
        floor_pose.orientation.w = pose.ori.w
        collisionObject.pose = floor_pose
        collisionObject.size_x = 15
        collisionObject.size_y = 15
        collisionObject.size_z = 0.1
        collisionObject.name = "floor"
        collisionObject.frame = "map"
        collisionObject.method = CollisionMethod.ADD
        collisionObject_list.append(collisionObject)

        # add walls around the robot

        collisionObject = CollisionObject()
        floor_pose = Pose()
        floor_pose.position.x = pose.pos.x + 1.5
        floor_pose.position.y = pose.pos.y
        floor_pose.position.z = 0.05
        floor_pose.orientation.w = pose.ori.w
        collisionObject.pose = floor_pose
        collisionObject.size_x = 0.01
        collisionObject.size_y = 4
        collisionObject.size_z = 0.1
        collisionObject.name = "front_wall"
        collisionObject.frame = "map"
        collisionObject.method = CollisionMethod.ADD
        collisionObject_list.append(collisionObject)

        collisionObject = CollisionObject()
        floor_pose = Pose()
        floor_pose.position.x = pose.pos.x - 1.5
        floor_pose.position.y = pose.pos.y
        floor_pose.position.z = 0.05
        floor_pose.orientation.w = pose.ori.w
        collisionObject.pose = floor_pose
        collisionObject.size_x = 0.01
        collisionObject.size_y = 4
        collisionObject.size_z = 0.1
        collisionObject.name = "behind_wall"
        collisionObject.frame = "map"
        collisionObject.method = CollisionMethod.ADD
        collisionObject_list.append(collisionObject)

        collisionObject = CollisionObject()
        floor_pose = Pose()
        floor_pose.position.x = pose.pos.x
        floor_pose.position.y = pose.pos.y + 2.0
        floor_pose.position.z = 0.05
        floor_pose.orientation.w = pose.ori.w
        collisionObject.pose = floor_pose
        collisionObject.size_x = 4
        collisionObject.size_y = 0.01
        collisionObject.size_z = 0.1
        collisionObject.name = "left_wall"
        collisionObject.frame = "map"
        collisionObject.method = CollisionMethod.ADD
        collisionObject_list.append(collisionObject)

        collisionObject = CollisionObject()
        floor_pose = Pose()
        floor_pose.position.x = pose.pos.x
        floor_pose.position.y = pose.pos.y - 1.0
        floor_pose.position.z = 0.05
        floor_pose.orientation.w = pose.ori.w
        collisionObject.pose = floor_pose
        collisionObject.size_x = 4
        collisionObject.size_y = 0.01
        collisionObject.size_z = 0.1
        collisionObject.name = "right_wall"
        collisionObject.frame = "map"
        collisionObject.method = CollisionMethod.ADD
        collisionObject_list.append(collisionObject)

        # send goal
        collisionEnvironment_goal.collisionObject_list = collisionObject_list
        collisionEnvironment_client.send_goal(collisionEnvironment_goal)
        collisionEnvironment_client.wait_for_result()
        collisionEnvironment_result = collisionEnvironment_client.get_result()

        if not collisionEnvironment_result.isDone:
            print("Error while creating the collision environment")

        rospy.sleep(2)

    def pointcloud_cb(self, data):
            self.cloud = data

    def get_table_collision_object(self):
        topic = rospy.get_param('/point_cloud_topic')
        sub = rospy.Subscriber(topic, PointCloud2, callback=self.pointcloud_cb)
        rospy.wait_for_message(topic, PointCloud2, timeout=15)
        rospy.wait_for_service('/test/table_plane_extractor')
        bb_list = []

        try:
            table_extractor = rospy.ServiceProxy('/test/table_plane_extractor', TablePlaneExtractor)
            response = table_extractor(self.cloud)
            for ros_bb in response.plane_bounding_boxes.boxes:
                bb_cloud = ros_bb_to_o3d_bb(ros_bb)

                if bb_cloud.center[2] < 0.2:
                    continue

                bb_cloud.color = (0, 1, 0)
                bb_cloud_mod = copy.deepcopy(bb_cloud)
                bb_cloud_mod.color = (0, 0, 1)

                bb_cloud_mod.center = (bb_cloud_mod.center[0], bb_cloud_mod.center[1], (bb_cloud_mod.center[2] + (bb_cloud_mod.extent[2] / 2)) / 2)
                bb_cloud_mod.extent = (bb_cloud_mod.extent[0]+0.04, bb_cloud_mod.extent[1]+0.04, bb_cloud.center[2] + (bb_cloud_mod.extent[2] / 2))

                bb_list.append(bb_cloud_mod)
                #print(bb_cloud_mod.center)
                #print(bb_cloud_mod.extent)
                #o3d.visualization.draw_geometries([cloud, bb_cloud, bb_cloud_mod])

        except rospy.ServiceException as e:
            print(e)

        return bb_list

    def add_box(self, name, position_x=0, position_y=0,
                position_z=0, size_x=0.1, size_y=0.1, size_z=0.1):
        """ Adds a box in the map frame to the MoveIt scene.

        Arguments:
            name {str}
            position_x {int} -- x coordinate in map frame (default: {0})
            position_y {int} -- y coordinate in map frame (default: {0})
            position_z {int} -- z coordinate in map frame (default: {0})
            size_x {float} -- size in x direction in meter (default: {0.1})
            size_y {float} -- size in y direction in meter (default: {0.1})
            size_z {float} -- size in z direction in meter (default: {0.1})
        """        
        rospy.sleep(0.2)
        box_pose = geometry_msgs.msg.PoseStamped()
        box_pose.header.frame_id = "map"
        box_pose.pose.orientation.w = 1.0
        box_pose.pose.position.x = position_x
        box_pose.pose.position.y = position_y
        box_pose.pose.position.z = position_z
        box_name = name
        self.scene.add_box(box_name, box_pose, size=(size_x, size_y, size_z))

    def create_collision_environment(self):
        """ Creates the collision environment by adding boxes to the MoveIt scene
        """        
        # if self.use_map:
        #    self.add_box('table', 0.39, -0.765, 0.175, 0.52, 0.52, 0.35)
        #    self.add_box('cupboard', 1.4, 1.1, 1, 2.5, 1, 2)
        #    self.add_box('desk', -1.5, -0.9, 0.4, 0.8, 1.8, 0.8)
        #    self.add_box('drawer', 0.2, -2, 0.253, 0.8, 0.44, 0.56)
        #    self.add_box('cupboard_2', 2.08, -1.23, 0.6, 0.6, 1.9, 1.2)
        self.add_box('floor', 0, 0, -0.1, 15, 15, 0.1)

    def add_marker(self, pose_goal):
        """ publishes a grasp marker to /grasping_pipeline/grasp_marker

        Arguments:
            pose_goal {geometry_msgs.msg.PoseStamped} -- pose for the grasp marker
        """
        br = tf.TransformBroadcaster()
        br.sendTransform((pose_goal.pose.position.x, pose_goal.pose.position.y, pose_goal.pose.position.z),
                         [pose_goal.pose.orientation.x, pose_goal.pose.orientation.y,
                             pose_goal.pose.orientation.z, pose_goal.pose.orientation.w],
                         rospy.Time.now(),
                         'grasp_pose_execute',
                         pose_goal.header.frame_id)

        marker_pub = rospy.Publisher(
            '/grasp_marker_2', Marker, queue_size=10, latch=True)
        marker = Marker()
        marker.header.frame_id = pose_goal.header.frame_id
        marker.header.stamp = rospy.Time()
        marker.ns = 'grasp_marker'
        marker.id = 0
        marker.type = 0
        marker.action = 0

        q2 = [pose_goal.pose.orientation.w, pose_goal.pose.orientation.x,
              pose_goal.pose.orientation.y, pose_goal.pose.orientation.z]
        q = tf.transformations.quaternion_about_axis(pi / 2, (0, 1, 0))
        q = tf.transformations.quaternion_multiply(q, q2)

        marker.pose.orientation.w = q[0]
        marker.pose.orientation.x = q[1]
        marker.pose.orientation.y = q[2]
        marker.pose.orientation.z = q[3]
        marker.pose.position.x = pose_goal.pose.position.x
        marker.pose.position.y = pose_goal.pose.position.y
        marker.pose.position.z = pose_goal.pose.position.z

        marker.scale.x = 0.1
        marker.scale.y = 0.05
        marker.scale.z = 0.01

        marker.color.a = 1.0
        marker.color.r = 0.0
        marker.color.g = 1.0
        marker.color.b = 0.0
        marker_pub.publish(marker)
        rospy.loginfo('grasp_marker')


def qv_mult(q, v):
    """ Rotating the vector v by quaternion q
    Arguments:
        q {list of float} -- Quaternion w,x,y,z
        v {list} -- Vector x,y,z

    Returns:
        numpy array -- rotated vector
    """    
    rot_mat = tf.transformations.quaternion_matrix(q)[:3, :3]
    v = np.array(v)
    return rot_mat.dot(v)


if __name__ == '__main__':
    rospy.init_node('execute_grasp_server')
    server = ExecuteGraspServer()
    rospy.spin()
