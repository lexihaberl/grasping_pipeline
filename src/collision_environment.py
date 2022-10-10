#! /usr/bin/env python3

import rospy
import actionlib
import tf
import tf2_ros
import moveit_commander
import smach
import copy
from hsrb_interface import Robot
from v4r_util.util import ros_bb_to_o3d_bb
from geometry_msgs.msg import PoseStamped, Pose, Vector3
from sensor_msgs.msg import PointCloud2
from grasping_pipeline.msg import CreateCollisionEnvironmentAction, CollisionObject, CreateCollisionEnvironmentResult, CreateCollisionEnvironmentGoal
from table_plane_extractor.srv import TablePlaneExtractor
from std_srvs.srv import Empty


class CreateCollisionEnvironmentServer:

    def __init__(self):
        self.robot = Robot()
        self.whole_body = self.robot.try_get('whole_body')
        self.gripper = self.robot.get('gripper')
        self.omni_base = self.robot.try_get('omni_base')
        self.tf = tf.TransformListener()

        self.robotCommander = moveit_commander.RobotCommander()
        self.scene = moveit_commander.PlanningSceneInterface()
        rospy.sleep(2.0)
        self.group_name = 'whole_body'
        self.move_group = moveit_commander.MoveGroupCommander(
            self.group_name, wait_for_servers=10.0)

        self.planning_frame = self.move_group.get_planning_frame()  # odom
        self.eef_link = self.move_group.get_end_effector_link()  # hand_palm_link

        self.move_group.allow_replanning(False)
        self.move_group.set_num_planning_attempts(2)
        self.move_group.set_goal_position_tolerance(0.01)

        # remove all objects
        self.scene.remove_attached_object(self.eef_link)
        self.scene.remove_world_object()
        self.move_group.clear_pose_targets()
        rospy.sleep(1)

        # server init
        self.server = actionlib.SimpleActionServer(
            'CreateCollisionEnvironmentServer', CreateCollisionEnvironmentAction, self.execute, False)

        # tf buffer for tf transform
        self.tf_buffer = tf2_ros.Buffer()
        self.tf_listener = tf2_ros.TransformListener(self.tf_buffer)

        self.clear_octomap = rospy.ServiceProxy('/clear_octomap', Empty)
        rospy.wait_for_service('/get_planning_scene', 10.0)

        self.server.start()

    def execute(self, goal):
        self.clear_octomap()
        rospy.loginfo('Execute ActionServer CollisionEnvironment')
        isDone = True

        for collisionObject in goal.collision_objects:
            size = (collisionObject.size.x,
                    collisionObject.size.y,
                    collisionObject.size.z)
            if collisionObject.frame == "eef_link":
                collisionObject.frame = self.eef_link
            if collisionObject.method == CollisionObject.METHOD_ADD_BOX:
                if self.add_box(collisionObject.name, collisionObject.pose, collisionObject.frame, size) is False:
                    isDone = False
            elif collisionObject.method == CollisionObject.METHOD_REMOVE_BOX:
                if self.remove_box(collisionObject.name) is False:
                    isDone = False
            elif collisionObject.method == CollisionObject.METHOD_ATTACH_BOX:
                group = "gripper"
                if self.attach_box(collisionObject.name, group, collisionObject.pose, collisionObject.frame, size) is False:
                    isDone = False
            elif collisionObject.method == CollisionObject.METHOD_DETACH_BOX:
                if self.detach_box(collisionObject.name) is False:
                    isDone = False

        self.result = CreateCollisionEnvironmentResult()
        self.result.isDone = isDone
        self.server.set_succeeded(self.result)

    def remove_box(self, name, timeout=4):
        self.scene.remove_world_object(name)

        return self.wait_for_state_update(box_is_attached=False, box_is_known=False, timeout=timeout, name=name)

    def add_box(self, name, pose, frame, size=(0.1, 0.1, 0.1), timeout=4):
        # add box to scene with pose
        box_name = name
        box_pose = PoseStamped()
        box_pose.header.frame_id = frame
        box_pose.pose = pose

        self.scene.add_box(box_name, box_pose, size)

        return self.wait_for_state_update(box_is_known=True, timeout=timeout, name=box_name)

    def attach_box(self, name, grasping_group, pose, frame, size=(0.1, 0.1, 0.1), timeout=4):
        poseStamped = PoseStamped()
        poseStamped.pose = pose
        poseStamped.header.frame_id = self.eef_link

        touch_links = self.robotCommander.get_link_names(group=grasping_group)
        self.scene.attach_box(self.eef_link, name,
                              poseStamped, size, touch_links=touch_links)

        return self.wait_for_state_update(box_is_attached=True, box_is_known=False, timeout=timeout, name=name)

    def detach_box(self, name, timeout=4):
        self.scene.remove_attached_object(self.eef_link, name=name)

        return self.wait_for_state_update(box_is_known=True, box_is_attached=False, timeout=timeout, name=name)

    def wait_for_state_update(self, box_is_known=False, box_is_attached=False, timeout=4, name="box"):
        # make sure that object is in scene
        box_name = name
        scene = self.scene

        start = rospy.get_time()
        seconds = rospy.get_time()
        while (seconds - start < timeout) and not rospy.is_shutdown():
            # Test if the box is in attached objects
            attached_objects = scene.get_attached_objects([box_name])
            is_attached = len(attached_objects.keys()) > 0

            # Test if the box is in the scene.
            # Note that attaching the box will remove it from known_objects
            is_known = box_name in scene.get_known_object_names()

            # Test if we are in the expected state
            if (box_is_attached == is_attached) and (box_is_known == is_known):
                return True

            # Sleep so that we give other threads time on the processor
            rospy.sleep(0.1)
            seconds = rospy.get_time()

        # If we exited the while loop without returning then we timed out
        return False


class CreateCollisionObjects(smach.State):

    def __init__(self):
        smach.State.__init__(self, outcomes=['succeeded', 'aborted'])
        self.robot = Robot()
        self.omni_base = self.robot.try_get('omni_base')
        self.topic = rospy.get_param('/point_cloud_topic')
        self.collision_env_client = actionlib.SimpleActionClient(
            'CreateCollisionEnvironmentServer', CreateCollisionEnvironmentAction)
        self.collision_env_client.wait_for_server()

    def execute(self, userdata):
        table_bbs = self.get_table_plane_bbs()

        collision_objects = list()

        try:
            idx = 0
            for table_bb in table_bbs.boxes:
                name = 'Table_plane_' + str(idx)
                idx = idx + 1
                table_bb.size.z = table_bb.size.z - 0.1
                coll_obj = self.create_collision_object(
                    name, table_bbs.header.frame_id, CollisionObject.METHOD_ADD_BOX, table_bb.center, table_bb.size)
                collision_objects.append(coll_obj)
        except rospy.ServiceException as e:
            print(e)
            return 'aborted'

        # Floor
        pose = self.omni_base.get_pose()
        pos = pose.pos
        ori = pose.ori
        coll_obj = self.create_collision_object_helper(
            'floor', 'map', CollisionObject.METHOD_ADD_BOX, [pos.x, pos.y, -0.06], ori.w, [15, 15, 0.1])
        collision_objects.append(coll_obj)

        # add walls around the robot
        coll_obj = self.create_collision_object_helper(
            'front_wall', 'map', CollisionObject.METHOD_ADD_BOX, [pos.x + 2.0, pos.y, 0.05], ori.w, [0.01, 4, 0.1])
        collision_objects.append(coll_obj)

        coll_obj = self.create_collision_object_helper(
            'back_wall', 'map', CollisionObject.METHOD_ADD_BOX, [pos.x - 1.5, pos.y, 0.05], ori.w, [0.01, 4, 0.1])
        collision_objects.append(coll_obj)

        coll_obj = self.create_collision_object_helper(
            'left_wall', 'map', CollisionObject.METHOD_ADD_BOX, [pos.x, pos.y + 2.0, 0.05], ori.w, [4, 0.01, 0.1])
        collision_objects.append(coll_obj)

        coll_obj = self.create_collision_object_helper(
            'right_wall', 'map', CollisionObject.METHOD_ADD_BOX, [pos.x, pos.y - 2.0, 0.05], ori.w, [4, 0.01, 0.1])
        collision_objects.append(coll_obj)

        goal = CreateCollisionEnvironmentGoal()
        # send goal
        goal.collision_objects = collision_objects
        self.collision_env_client.send_goal(goal)
        self.collision_env_client.wait_for_result()
        collision_env_result = self.collision_env_client.get_result()

        if not collision_env_result.isDone:
            print("Error while creating the collision environment")
        rospy.sleep(2)
        return 'succeeded'

    def get_table_plane_bbs(self):
        cloud = rospy.wait_for_message(self.topic, PointCloud2, timeout=15)
        rospy.wait_for_service('/test/table_plane_extractor')

        table_extractor = rospy.ServiceProxy(
            '/test/table_plane_extractor', TablePlaneExtractor)
        response = table_extractor(cloud)
        for ros_bb in response.plane_bounding_boxes.boxes:
            center = ros_bb.center.position
            old_center_z = center.z
            size = ros_bb.size
            center.z = (center.z + size.z/2)/2
            size.x = size.x + 0.04
            size.y = size.y + 0.04
            size.z = old_center_z + size.z/2

        return response.plane_bounding_boxes

    def create_collision_object(self, name, frame, method, pose, size):
        coll_obj = CollisionObject()
        coll_obj.name = name
        coll_obj.frame = frame
        coll_obj.method = method
        coll_obj.pose = pose
        coll_obj.size = size
        return coll_obj

    def create_collision_object_helper(self, name, frame, method, translation, orientation_w, size):
        pose = Pose()
        pose.position.x = translation[0]
        pose.position.y = translation[1]
        pose.position.z = translation[2]
        pose.orientation.w = orientation_w
        size = Vector3(size[0], size[1], size[2])
        return self.create_collision_object(name, frame, method, pose, size)


if __name__ == '__main__':
    rospy.init_node('Execute_ActionServer_CollisionEnvironment')
    server = CreateCollisionEnvironmentServer()
    rospy.spin()